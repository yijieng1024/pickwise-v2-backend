"""
Family assignment and family deduplication — the two things the laptop_family
table exists to do, in one module so the rule cannot drift between them.

ASSIGNMENT (resolve_family_id / regroup_unassigned) answers "which product line
is this configuration part of?". DEDUPLICATION (deduplicate_by_family) answers
"which one member of that line should this ranked list show?" — the catalog
stores one row per configuration, so a single machine can otherwise fill most
of a six-result shortlist on its own.

The one rule both halves are built around: NEVER GUESS A FAMILY. A null
family_id passes through deduplication untouched and costs a little shortlist
space; a wrong family_id hides a machine the user could have bought, and does
it invisibly. So when the evidence is ambiguous this module leaves the laptop
unassigned and lets POST /families/regroup report it as backlog.
"""
import uuid
from collections import defaultdict
from typing import Callable, Iterable, Optional, TypeVar

from sqlalchemy import func, select as sa_select
from sqlmodel import Session

from app.laptops.family_key import family_key
from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop

T = TypeVar("T")


# --- Assignment --------------------------------------------------------------

def resolve_family_id(
    session: Session,
    product_name: str,
    exclude_laptop_id: Optional[uuid.UUID] = None,
) -> Optional[uuid.UUID]:
    """
    The family a newly ingested laptop belongs to, or None when that cannot be
    established from evidence already in the table.

    Note what this does NOT do: look the family up by `family_key` equality.
    A family's stored key is provenance only — after an admin merges
    "14-inch macbook pro" and "16-inch macbook pro" into one MacBook Pro
    family, that family carries one of the two keys, and key equality would
    strand every future 16-inch config. Instead this asks the laptops: find
    the rows that already share this seed key and already have a family, and
    adopt theirs.

    Disagreement — the seed key spans two families, which is exactly what a
    partial merge looks like — returns None rather than picking a side.
    """
    key = family_key(product_name)

    stmt = sa_select(Laptop.family_id, Laptop.product_name).where(
        Laptop.family_id.is_not(None)  # type: ignore[union-attr]
    )
    if exclude_laptop_id is not None:
        stmt = stmt.where(Laptop.id != exclude_laptop_id)

    # The seed key is computed in Python (it truncates at the first paren, then
    # lowercases and collapses whitespace), so the match cannot be pushed into
    # SQL. Scanning the assigned rows of one catalog is cheap, and beats a
    # denormalised key column that would go stale on every product_name edit.
    found: set[uuid.UUID] = set()
    for family_id, name in session.execute(stmt).all():
        if family_key(name) == key:
            found.add(family_id)

    return found.pop() if len(found) == 1 else None


def move_laptops(
    session: Session,
    laptop_ids: Iterable[uuid.UUID],
    target_family_id: Optional[uuid.UUID],
) -> dict:
    """
    Move several laptops into one family at once, or release them all to
    unassigned when `target_family_id` is None.

    This is the write half of every merge, so it lives here rather than in the
    router: both membership endpoints call it, and the two rules below must
    not drift apart between them.

      - ALL OR NOTHING on unknown ids. A merge that applied to nine of ten
        selected laptops leaves a family half-moved and no record of which
        half, so an unknown id writes nothing and is handed back to the caller
        to turn into a 404. (Duplicate ids in one request are the same
        assignment twice and are collapsed instead.)
      - The SOURCE families a move empties are reported, because deleting an
        emptied family is the second half of the merge and the caller cannot
        see it from the target family alone. They are reported, never deleted:
        removing a family is an explicit admin action (DELETE /families/{id}),
        and an empty family is a legitimate state — it is what
        POST /families creates.

    Returns {moved, unchanged, missing, emptied_family_ids}, where `unchanged`
    counts laptops already in the target (re-sending a selection is a no-op,
    not an error) and `missing` is empty on any run that wrote anything.
    """
    wanted = list(dict.fromkeys(laptop_ids))
    if not wanted:
        return {"moved": 0, "unchanged": 0, "missing": [], "emptied_family_ids": []}

    found = {
        laptop.id: laptop
        for laptop in session.execute(
            sa_select(Laptop).where(Laptop.id.in_(wanted))  # type: ignore[union-attr]
        ).scalars().all()
    }
    missing = [i for i in wanted if i not in found]
    if missing:
        return {
            "moved": 0,
            "unchanged": 0,
            "missing": missing,
            "emptied_family_ids": [],
        }

    # Captured before the write, and with the target excluded — a laptop that
    # never left the target family cannot have emptied it.
    sources = {
        laptop.family_id for laptop in found.values() if laptop.family_id is not None
    } - {target_family_id}

    moved = unchanged = 0
    for laptop in found.values():
        if laptop.family_id == target_family_id:
            unchanged += 1
            continue
        laptop.family_id = target_family_id
        session.add(laptop)
        moved += 1

    emptied: list[uuid.UUID] = []
    if sources:
        # The count below has to see the reassignments, and they are still
        # pending in the session until flushed.
        session.flush()
        remaining = dict(
            session.execute(
                sa_select(Laptop.family_id, func.count(Laptop.id))
                .where(Laptop.family_id.in_(sources))  # type: ignore[union-attr]
                .group_by(Laptop.family_id)
            ).all()
        )
        emptied = sorted(
            (fid for fid in sources if remaining.get(fid, 0) == 0), key=str
        )

    session.commit()
    return {
        "moved": moved,
        "unchanged": unchanged,
        "missing": [],
        "emptied_family_ids": emptied,
    }


def regroup_unassigned(session: Session) -> dict:
    """
    Auto-group every laptop with a null family_id, and nothing else.

    Skipping assigned laptops is what makes this safe to re-run and unable to
    undo a manual decision: a merge an admin performed through the CRUD is
    invisible here, because none of its members are null any more.

    Per seed key, in order:
      1. Assigned laptops already share this key and agree → adopt that family.
         This is what carries a merge forward: once one MacBook Pro family
         holds both size groups, the next 16-inch config joins it by itself.
      2. They disagree → leave null. The key straddles a boundary a human drew.
      3. Nobody shares the key → create a family for it and assign the group.

    Returns {families_created, laptops_assigned, left_null}.
    """
    unassigned = list(
        session.execute(
            sa_select(Laptop)
            .where(Laptop.family_id.is_(None))  # type: ignore[union-attr]
            # Names tie constantly (14 configs of one machine); id is the
            # total order that keeps a re-run's report diffable.
            .order_by(Laptop.product_name, Laptop.id)
        ).scalars().all()
    )

    # One pass over the assigned rows up front, rather than a query per key.
    assigned_by_key: dict[str, set[uuid.UUID]] = defaultdict(set)
    for family_id, name in session.execute(
        sa_select(Laptop.family_id, Laptop.product_name).where(
            Laptop.family_id.is_not(None)  # type: ignore[union-attr]
        )
    ).all():
        assigned_by_key[family_key(name)].add(family_id)

    groups: dict[str, list[Laptop]] = defaultdict(list)
    for laptop in unassigned:
        groups[family_key(laptop.product_name)].append(laptop)

    created = assigned = left_null = 0

    for key in sorted(groups):
        members = groups[key]
        existing = assigned_by_key.get(key, set())

        if len(existing) > 1:
            left_null += len(members)
            continue

        if len(existing) == 1:
            target_id = next(iter(existing))
        else:
            family = LaptopFamily(
                brand_id=members[0].brand_id,
                # The product name with its original casing, truncated at the
                # first paren: a readable starting name, which the admin
                # renames when merging up to the real product line.
                name=members[0].product_name.split("(")[0].strip(),
                family_key=key,
                is_verified=False,
            )
            session.add(family)
            session.flush()  # need the generated id below
            target_id = family.id
            assigned_by_key[key] = {target_id}
            created += 1

        for laptop in members:
            laptop.family_id = target_id
            session.add(laptop)
            assigned += 1

    session.commit()
    return {
        "families_created": created,
        "laptops_assigned": assigned,
        "left_null": left_null,
    }


# --- Deduplication -----------------------------------------------------------

def deduplicate_by_family(
    items: Iterable[T],
    family_id_of: Callable[[T], Optional[uuid.UUID]],
    prefer: Optional[Callable[[T, T], T]] = None,
) -> list[T]:
    """
    Collapse an already-ordered list to one item per family, best first.

    Two properties matter and are easy to lose:

      - A family keeps the RANK POSITION of its best-placed member, even when
        `prefer` swaps in a different member as the representative. Appending
        the winner instead would let deduplication quietly reorder the list it
        was handed, which is the reranker's job, not this function's.
      - A null family_id passes straight through. Unassigned laptops are not
        one family of unknowns; they are individually unknown, and grouping
        them would hide real products behind an artefact of missing data.

    `prefer(incumbent, challenger)` returns whichever should represent the
    family; it defaults to keeping the incumbent, i.e. the best-ranked member.
    """
    out: list[T] = []
    position: dict[uuid.UUID, int] = {}

    for item in items:
        fid = family_id_of(item)
        if fid is None:
            out.append(item)
            continue

        seen_at = position.get(fid)
        if seen_at is None:
            position[fid] = len(out)
            out.append(item)
            continue

        if prefer is not None:
            out[seen_at] = prefer(out[seen_at], item)

    return out


def closest_to_budget(
    price_of: Callable[[T], float], budget: float
) -> Callable[[T, T], T]:
    """
    A `prefer` function that represents each family with the configuration
    nearest the stated budget.

    This is what makes a wide price range inside one family harmless: the
    family shows a different member per query instead of always its
    best-ranked (usually cheapest-matching) config. Retrieval applies budget as
    a hard filter, so in the common case every candidate is already under it
    and "nearest" resolves to the most capable configuration affordable.
    Absolute distance rather than "highest under budget" because constraint
    relaxation can raise the ceiling and leave candidates on both sides of it.
    """
    def prefer(incumbent: T, challenger: T) -> T:
        a = abs(price_of(incumbent) - budget)
        b = abs(price_of(challenger) - budget)
        # A tie keeps the incumbent: it is the better-ranked of the two, and
        # "keep what is already there" is deterministic where a coin flip
        # between two equal prices is not.
        return challenger if b < a else incumbent

    return prefer
