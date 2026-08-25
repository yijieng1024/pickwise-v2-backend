"""
Query helpers for the human review-linking screen (ADR-0012).

The one non-obvious piece here is `differing_columns`. A laptop family holds up
to 14 configurations of one machine, and a full spec sheet rendered 14 times is
unreadable — the reviewer only needs the fields that actually tell the
configurations apart. For a TUF F16 that is CPU / GPU / RAM; screen size,
weight, battery and ports are identical across every member and are pure noise
on the screen.

So the config list computes, per family, which spec columns have more than one
distinct value among that family's members, and returns only those. It is
derived per family rather than configured globally because the answer differs:
an Apple family varies by chip and storage, a gaming family by GPU, and an
ExpertBook family sometimes by nothing at all.
"""
import re
import uuid
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop
from app.reviews.link_model import MatchSource, ReviewLaptopLink

# Candidate columns for the "what differs" computation, in the order a human
# reads a spec sheet. Deliberately a curated list rather than every column on
# the table: `id`, `created_at`, `raw_specs` and `image_urls` differ on every
# row and would drown the real signal, and `product_name` / `model_code` are
# shown as the row label anyway.
_SPEC_COLUMNS: tuple[str, ...] = (
    "price_rm",
    "processor_model",
    "gpu_model",
    "ram_gb",
    "ssd_gb",
    "display_size_inch",
    "display_resolution",
    "display_refresh_rate_hz",
    "display_type",
    "touchscreen",
    "weight_kg",
    "battery_wh",
    "os",
    "colors",
    "release_year",
)

# Human-readable labels so the screen does not have to carry its own mapping
# and drift from the column names.
_COLUMN_LABELS: dict[str, str] = {
    "price_rm": "Price (RM)",
    "processor_model": "CPU",
    "gpu_model": "GPU",
    "ram_gb": "RAM (GB)",
    "ssd_gb": "Storage (GB)",
    "display_size_inch": "Screen (in)",
    "display_resolution": "Resolution",
    "display_refresh_rate_hz": "Refresh (Hz)",
    "display_type": "Panel",
    "touchscreen": "Touch",
    "weight_kg": "Weight (kg)",
    "battery_wh": "Battery (Wh)",
    "os": "OS",
    "colors": "Colour",
    "release_year": "Year",
}


def _hashable(value: Any) -> Any:
    """Column values are compared for distinctness, and a couple of them are
    lists (`colors`), which are unhashable. Normalise to something a set can
    hold without changing what 'distinct' means."""
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


def differing_columns(members: list[Laptop]) -> list[str]:
    """Which spec columns actually distinguish these configurations.

    A column counts as differing when its members hold more than one distinct
    value. NULL is a value here, not a blank: a family where half the rows
    record a battery figure and half do not IS differing on battery, and hiding
    that would present the two as interchangeable.

    A one-member family returns [] — nothing distinguishes a single row, and
    the screen should show the product name alone.
    """
    if len(members) < 2:
        return []
    out = []
    for column in _SPEC_COLUMNS:
        values = {_hashable(getattr(laptop, column, None)) for laptop in members}
        if len(values) > 1:
            out.append(column)
    return out


# Columns a laptop review essentially never states. Not "rarely" — a reviewer
# is sent one unit, its RAM and storage are the parts of the spec that change
# nothing about how the machine performs in the ways a review discusses, and no
# conclusion in the video would differ if it had 32GB instead of 64GB.
_UNSTATEABLE_COLUMNS = frozenset({"ram_gb", "ssd_gb"})


def separability(
    columns: list[str], member_count: int
) -> tuple[bool, str, Optional[str]]:
    """Can a human actually tell these configurations apart from a video?

    Returns (separable, code, reason). Not separable means the screen must NOT
    render a chooser — it shows the reason and leaves the selection unset.

    The `code` is there so the screen branches on a value rather than on
    English: the three unseparable cases are not the same case. RAM_STORAGE_ONLY
    is unanswerable in principle and the reason is the whole message.
    SINGLE_CONFIG is different in kind — there is nothing to choose BETWEEN, but
    the one row may still be the right link if the video names it, so a screen
    may reasonably offer it as a single confirmable row rather than hiding it.
    Collapsing the three into one boolean would force that distinction to be
    recovered by string matching.

    This is the part that matters most on that screen. Presenting four options
    when the discriminating information cannot exist in a review does not
    merely inconvenience the human, it invites a guess, and a guessed laptop_id
    is strictly worse than a null one: null is honest and reads downstream as
    "configuration unknown", while a guess silently attaches this video's
    performance claims to a machine the reviewer never touched. The whole
    family-first design of ADR-0012 exists because the configuration is
    frequently unknowable; this is that principle applied to the one screen
    that was still asking anyway.

    A family separable on CPU or GPU stays separable even if it ALSO differs in
    RAM — the human can answer the CPU question and the RAM question rides
    along with it. Only a family whose entire remaining difference is RAM and
    storage is unanswerable in principle.
    """
    if member_count == 0:
        # Reachable, and not a data error: the ExpertBook P3 G2 family is two
        # rows and both are suspended, so the config filter empties it. Given a
        # distinct code because the honest message is about the catalog, not
        # about the video — the caller knows whether suspension is the cause and
        # says so.
        return False, "no_configs", (
            "No linkable configuration of this product line is in the catalog."
        )
    if member_count < 2:
        return False, "single_config", (
            "Only one configuration of this product line is in the catalog, so "
            "there is nothing to choose between. That on its own is not "
            "evidence the reviewer tested it."
        )
    if not columns:
        return False, "identical_specs", (
            "The configurations in this line are identical in every spec we "
            "track, so there is nothing to choose between."
        )
    if set(columns) <= _UNSTATEABLE_COLUMNS:
        return False, "ram_storage_only", (
            "The configurations in this line differ only in RAM and storage, "
            "which reviews rarely specify. Leave the configuration unset."
        )
    return True, "separable", None


def family_members(
    session: Session,
    family_id: uuid.UUID,
    exclude_statuses: tuple[str, ...] = (),
) -> list[Laptop]:
    """A family's configurations, in a stable order.

    Ordered by name then id: the id tiebreak matters because sibling configs
    routinely share a product_name (they differ only in the spec columns), and
    an unbroken tie falls through to database return order, which would
    reshuffle the human's list between two loads of the same screen.

    Unfiltered by default — an admin linking a review needs to see every
    configuration, including retired ones, because the review may well be of a
    machine that has since been withdrawn. That is exactly why `inactive` is
    NOT excluded anywhere: a delisted laptop is the normal subject of an old
    review, and hiding it would make those reviews unlinkable.

    `exclude_statuses` is for the one case that is different. The config
    chooser passes SUSPENDED: a suspended row is a listing on hold, and in
    practice it carries placeholder data — the row that prompted this was
    priced RM 0, which is the catalog's "unknown" showing through as "free".
    Asking a human to attach review evidence to it is asking them to file
    evidence against a record nobody trusts. Excluding it also changes the
    answer downstream, because differing_columns is recomputed over what
    remains: dropping that RM 0 row collapsed price to a constant 11999 and
    removed a column that looked discriminating and was not.
    """
    statement = select(Laptop).where(Laptop.family_id == family_id)
    if exclude_statuses:
        statement = statement.where(
            Laptop.status.notin_(exclude_statuses)  # type: ignore[attr-defined]
        )
    return list(
        session.exec(
            statement.order_by(Laptop.product_name, Laptop.id)  # type: ignore[arg-type]
        ).all()
    )


def links_for_reviews(
    session: Session, review_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict]]:
    """All links for a page of reviews, resolved to names, in three queries.

    Grouped by review id. Built as one batch rather than per review so the
    queue endpoint does not issue a query per row — the screen shows the whole
    pending queue and the N+1 would be the page's dominant cost.
    """
    if not review_ids:
        return {}

    links = list(
        session.exec(
            select(ReviewLaptopLink)
            .where(ReviewLaptopLink.raw_review_id.in_(review_ids))  # type: ignore[attr-defined]
            .order_by(ReviewLaptopLink.created_at, ReviewLaptopLink.id)  # type: ignore[arg-type]
        ).all()
    )
    if not links:
        return {}

    family_names = dict(
        session.execute(
            sa_select(LaptopFamily.id, LaptopFamily.name).where(
                LaptopFamily.id.in_({link.family_id for link in links})  # type: ignore[attr-defined]
            )
        ).all()
    )
    laptop_ids = {link.laptop_id for link in links if link.laptop_id}
    laptop_names = (
        dict(
            session.execute(
                sa_select(Laptop.id, Laptop.product_name).where(
                    Laptop.id.in_(laptop_ids)  # type: ignore[attr-defined]
                )
            ).all()
        )
        if laptop_ids
        else {}
    )

    grouped: dict[uuid.UUID, list[dict]] = {}
    for link in links:
        grouped.setdefault(link.raw_review_id, []).append(
            {
                "id": link.id,
                "raw_review_id": link.raw_review_id,
                "family_id": link.family_id,
                "family_name": family_names.get(link.family_id),
                "laptop_id": link.laptop_id,
                # None for a family-only link AND for a deleted laptop; the
                # caller tells them apart by looking at laptop_id.
                "laptop_name": (
                    laptop_names.get(link.laptop_id) if link.laptop_id else None
                ),
                "match_source": link.match_source,
                "match_confidence": link.match_confidence,
                "tie_width": link.tie_width,
                "created_at": link.created_at,
            }
        )
    return grouped


def column_label(column: str) -> str:
    return _COLUMN_LABELS.get(column, column)


# What a person names a configuration by, in the order they say it. Anything
# outside this list is a tiebreak, not a name.
_LABEL_COLUMNS: tuple[str, ...] = (
    "processor_model",
    "gpu_model",
    "ram_gb",
    "ssd_gb",
    "display_size_inch",
)
_LABEL_MAX_PARTS = 4


def config_label(laptop: Laptop, columns: list[str]) -> str:
    """A short human row label built from what actually differs.

    "Ultra 7 358H / Arc B390 / 32GB / 1TB" — the same string the screen shows
    back as "Selected: ...", so the label the human picked and the label they
    are shown afterwards cannot disagree.

    This replaces `model_code` as the row identity on that screen. model_code is
    a database key: `asus-expertbook-ultra-b9406caa-ultra7-358h-arcb390-32gb-1tb`
    is unreadable at a glance and, worse, is a slug of the CPU/GPU/RAM/Storage
    columns rendered in human form immediately beside it. It said nothing the
    row did not already say, in the least readable way available.

    Built from at most `_LABEL_MAX_PARTS` columns, drawn from `_LABEL_COLUMNS`
    in that order rather than from every differing column. A wide family makes
    the difference stark: the ROG Zephyrus G16 differs in eight tracked columns,
    and naming a row by all of them produces "11299.0 / Ultra 9 285H / RTX 5070
    Laptop GPU / 32GB / 1TB / ROG Nebula Display OLED / 1.85 / 2025" — eight
    wrapped lines per row, in a column sitting beside the same eight values in
    their own cells. A label is a name, not a spec sheet; the table is the spec
    sheet.

    The priority order is how a person names a configuration out loud — chip
    first, then graphics, then memory. Price is deliberately excluded even
    though it often differs: it is a number with no identity, it changes without
    the machine changing, and it is what a price column is for.

    Falls back to any other differing columns when none of the preferred ones
    differ (a family separated only by colour still needs a name), and to the
    product name when nothing differs at all.
    """
    preferred = [c for c in _LABEL_COLUMNS if c in columns]
    rest = [c for c in columns if c not in _LABEL_COLUMNS and c != "price_rm"]
    ordered = (preferred or rest)[:_LABEL_MAX_PARTS]

    parts = [
        _display_value(column, getattr(laptop, column, None))
        for column in ordered
        if getattr(laptop, column, None) is not None
    ]
    return " / ".join(p for p in parts if p) or laptop.product_name


def _display_value(column: str, value: Any) -> str:
    """Compact rendering for a label. Storage in TB past 1024 because that is
    how the number is written everywhere a human will check it against."""
    if value is None:
        return ""
    if column == "ssd_gb" and isinstance(value, int) and value >= 1024 and value % 1024 == 0:
        return f"{value // 1024}TB"
    if column == "ssd_gb":
        return f"{value}GB"
    if column == "ram_gb":
        return f"{value}GB"
    if column == "processor_model":
        # Drop the vendor prefix a spec sheet repeats on every row.
        return re.sub(
            r"^(Intel|AMD|Apple)\s+(Core\s+)?(Processor\s+)?", "", str(value)
        ).replace("Processor ", "")
    if column == "gpu_model":
        return re.sub(r"^(NVIDIA|Intel|AMD)\s+(GeForce\s+)?", "", str(value))
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def config_row(laptop: Laptop, columns: list[str]) -> dict:
    """One configuration, carrying only the columns that differ in its family."""
    return {
        "laptop_id": laptop.id,
        "product_name": laptop.product_name,
        "label": config_label(laptop, columns),
        "status": laptop.status,
        "specs": {column: getattr(laptop, column, None) for column in columns},
    }


def mark_indistinguishable(rows: list[dict], columns: list[str]) -> int:
    """Flag config rows that are identical to another row in every shown column.

    Mutates `rows`, returns how many were flagged.

    This is not cosmetic. The ROG Zephyrus G16 family holds two pairs of rows
    that match on all eight tracked columns — genuine duplicate catalog entries,
    distinct laptop_ids with nothing to tell them apart. Rendered plainly they
    are two identical radio options, and choosing between them is a coin flip:
    the same "invited guess" this screen exists to remove, arriving through the
    data rather than through the question.

    They are flagged rather than merged or hidden. Merging would be a lie about
    the catalog and would silently drop a laptop_id that other tables reference;
    hiding one would make the choice for the human without saying so. The honest
    move is to show both and say they are indistinguishable, so the human knows
    the pick is arbitrary — and so the duplicate is visible as something to fix
    in the catalog rather than something to work around here forever.
    """
    if not columns:
        return 0
    signatures: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(_hashable(row["specs"].get(column)) for column in columns)
        signatures.setdefault(key, []).append(row)

    flagged = 0
    for group in signatures.values():
        duplicate = len(group) > 1
        for row in group:
            row["indistinguishable"] = duplicate
            if duplicate:
                flagged += 1
    return flagged


def find_family_id(session: Session, laptop_id: uuid.UUID) -> Optional[uuid.UUID]:
    """The family a laptop belongs to, or None if it has not been grouped."""
    laptop = session.get(Laptop, laptop_id)
    return laptop.family_id if laptop else None


def create_human_link(
    session: Session,
    raw_review_id: uuid.UUID,
    family_id: uuid.UUID,
    laptop_id: Optional[uuid.UUID],
) -> ReviewLaptopLink:
    """Validate and build one human link. Shared by both write paths.

    There are two endpoints that create a human link — POST
    /reviews/{id}/links (the new screen) and PATCH /reviews/raw/{id}/match (the
    existing manual match) — and they must agree on every guard. Two divergent
    copies of a cross-family check is how one of them silently stops matching
    the other, so the checks live here once and both routes call this.

    It raises HTTPException rather than a domain error, which is a deliberate
    exception to keeping services framework-free: the whole point is that both
    routes return byte-identical 400/404/409 responses, and translating a
    domain error twice is the duplication this function exists to remove.

    Adds to the session but does NOT commit — the caller decides, because the
    manual-match path writes matched_laptop_id in the same transaction.
    """
    family = session.get(LaptopFamily, family_id)
    if not family:
        raise HTTPException(status_code=404, detail="Family not found.")

    if laptop_id is not None:
        laptop = session.get(Laptop, laptop_id)
        if not laptop:
            raise HTTPException(status_code=404, detail="Laptop not found.")
        # The configuration must belong to the family being linked, or the row
        # asserts two contradictory things about one review and any reader that
        # trusts one of the two columns gets a different answer.
        if laptop.family_id != family_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Laptop {laptop_id} belongs to family {laptop.family_id}, "
                    f"not {family_id}."
                ),
            )

    # Checked here rather than left to the unique constraint: Postgres treats
    # NULLs as distinct in a unique index, so (review, family, NULL) can be
    # inserted twice and uq_review_family_laptop would not catch it at all.
    # Doing it explicitly also turns the known-config collision into a usable
    # 409 instead of a raw IntegrityError 500.
    duplicate = session.exec(
        select(ReviewLaptopLink)
        .where(ReviewLaptopLink.raw_review_id == raw_review_id)
        .where(ReviewLaptopLink.family_id == family_id)
        .where(
            ReviewLaptopLink.laptop_id.is_(None)  # type: ignore[union-attr]
            if laptop_id is None
            else ReviewLaptopLink.laptop_id == laptop_id
        )
    ).first()
    if duplicate:
        raise HTTPException(
            status_code=409,
            detail=(
                "This review is already linked to that family/config "
                f"(link {duplicate.id})."
            ),
        )

    link = ReviewLaptopLink(
        raw_review_id=raw_review_id,
        family_id=family_id,
        laptop_id=laptop_id,
        # Both callers are human actions. match_confidence stays NULL because a
        # human did not score anything — writing a fabricated 100.0 is exactly
        # what made manual matches indistinguishable from perfect fuzzy scores
        # in the column this table replaces.
        match_source=MatchSource.HUMAN.value,
        match_confidence=None,
        tie_width=None,
    )
    session.add(link)
    return link


def resolve_family_for_laptop(
    session: Session, laptop_id: uuid.UUID
) -> uuid.UUID:
    """The family a laptop belongs to, for callers that supply only a laptop.

    404 when the laptop is unknown; 400 when it exists but has no family, which
    is a real and valid state (`family_id` is nullable — see family_service),
    and one a human must resolve by grouping the laptop first. Guessing a
    family here would put a wrong grouping behind a human's name.
    """
    laptop = session.get(Laptop, laptop_id)
    if not laptop:
        raise HTTPException(status_code=404, detail="Laptop not found.")
    if laptop.family_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Laptop {laptop_id} has no family yet, so the review cannot be "
                "linked to a product line. Assign it a family first "
                "(POST /families/regroup or the families CRUD), or pass "
                "family_id explicitly."
            ),
        )
    return laptop.family_id
