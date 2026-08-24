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
import uuid
from typing import Any, Optional

from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop
from app.reviews.link_model import ReviewLaptopLink

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


def family_members(session: Session, family_id: uuid.UUID) -> list[Laptop]:
    """A family's configurations, in a stable order.

    Ordered by name then id: the id tiebreak matters because sibling configs
    routinely share a product_name (they differ only in the spec columns), and
    an unbroken tie falls through to database return order, which would
    reshuffle the human's list between two loads of the same screen.

    No status filter — an admin linking a review needs to see every
    configuration, including retired ones, because the review may well be of a
    machine that has since been withdrawn.
    """
    return list(
        session.exec(
            select(Laptop)
            .where(Laptop.family_id == family_id)
            .order_by(Laptop.product_name, Laptop.id)  # type: ignore[arg-type]
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


def config_row(laptop: Laptop, columns: list[str]) -> dict:
    """One configuration, carrying only the columns that differ in its family."""
    return {
        "laptop_id": laptop.id,
        "product_name": laptop.product_name,
        "model_code": laptop.model_code,
        "status": laptop.status,
        "specs": {column: getattr(laptop, column, None) for column in columns},
    }


def find_family_id(session: Session, laptop_id: uuid.UUID) -> Optional[uuid.UUID]:
    """The family a laptop belongs to, or None if it has not been grouped."""
    laptop = session.get(Laptop, laptop_id)
    return laptop.family_id if laptop else None
