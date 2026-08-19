"""
Admin CRUD for laptop families, plus the regroup run that seeds them.

Auto-grouping (POST /families/regroup) only ever produces a STARTING POINT —
the seed key splits finer than the taxonomy wants, so several seed families
usually belong to one product line. Merging them up is what these endpoints
are for, and a merge is spelled: move the members with
POST /families/{keeper}/laptops, then DELETE the emptied family.

Read routes are public, writes are admin-only — the same split as
brand_router.py and the taxonomy routers.

Param naming: `laptop_router.py` had to alias its `status` filter to
`status_filter` because a query param named `status` shadows FastAPI's
`status` import inside the module. Nothing here is named after an import
(`brand_id`, `is_verified`, `family_id`, `laptop_id`), so no alias is needed —
but the same collision applies to any param added later.
"""
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select as sa_select
from sqlmodel import Session

from app.database import get_session
from app.laptops.brand_model import LaptopBrand
from app.laptops.family_key import family_key
from app.laptops.family_model import (
    FamilyCreate,
    FamilyDetail,
    FamilyLaptopsAssign,
    FamilyMember,
    FamilyRead,
    FamilyUpdate,
    LaptopFamily,
    RegroupResult,
    UnassignedLaptop,
    UnassignedSummary,
)
from app.laptops.family_service import regroup_unassigned
from app.laptops.laptop_models import Laptop
from app.logger import get_logger
from app.users.auth import get_current_admin

logger = get_logger(__name__)

router = APIRouter(prefix="/families", tags=["Laptop Families"])


def _member_counts(session: Session) -> dict[uuid.UUID, int]:
    rows = session.execute(
        sa_select(Laptop.family_id, func.count(Laptop.id))
        .where(Laptop.family_id.is_not(None))  # type: ignore[union-attr]
        .group_by(Laptop.family_id)
    ).all()
    return {family_id: count for family_id, count in rows}


def _to_read(family: LaptopFamily, brand_name: str, member_count: int) -> FamilyRead:
    return FamilyRead(
        id=family.id,
        brand_id=family.brand_id,
        brand_name=brand_name,
        name=family.name,
        family_key=family.family_key,
        is_verified=family.is_verified,
        member_count=member_count,
        created_at=family.created_at,
        updated_at=family.updated_at,
    )


def _get_family(session: Session, family_id: uuid.UUID) -> LaptopFamily:
    family = session.get(LaptopFamily, family_id)
    if not family:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Family not found"
        )
    return family


def _assert_brand_exists(session: Session, brand_id: uuid.UUID) -> LaptopBrand:
    brand = session.get(LaptopBrand, brand_id)
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found"
        )
    return brand


def _assert_name_free(
    session: Session,
    brand_id: uuid.UUID,
    name: str,
    exclude_id: Optional[uuid.UUID] = None,
) -> None:
    """Two families of one brand sharing a name is always a mistake — usually a
    half-finished merge, where reassigning members to "the" MacBook Pro family
    would be a coin flip. Names may repeat ACROSS brands (Acer and ASUS both
    ship a "Swift"-ish line), so the check is scoped to the brand."""
    clash = session.execute(
        sa_select(LaptopFamily.id)
        .where(LaptopFamily.brand_id == brand_id)
        .where(func.lower(LaptopFamily.name) == name.lower())
    ).scalars().all()
    if any(fid != exclude_id for fid in clash):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This brand already has a family named '{name}'",
        )


# --- Static paths first ------------------------------------------------------
# /unassigned and /regroup must be declared above /{family_id}, or FastAPI
# matches the path param first and 422s trying to parse them as a UUID.

@router.get("/unassigned", response_model=UnassignedSummary)
def list_unassigned(
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    """
    Laptops with no family — the backlog POST /families/regroup exists to
    clear, and the number the admin screens show without needing a query.

    `count` is the true total; `laptops` is capped by `limit`, so a large
    backlog still reports its real size instead of the size of one page.
    """
    count = session.execute(
        sa_select(func.count(Laptop.id)).where(Laptop.family_id.is_(None))  # type: ignore[union-attr]
    ).scalar_one()

    rows = session.execute(
        sa_select(Laptop, LaptopBrand.name)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)  # type: ignore[arg-type]
        .where(Laptop.family_id.is_(None))  # type: ignore[union-attr]
        .order_by(Laptop.product_name, Laptop.id)
        .limit(limit)
    ).all()

    # Sibling counts come from the whole backlog, not the page — "3 others
    # share this key" must not shrink because the page cut them off.
    all_keys = session.execute(
        sa_select(Laptop.product_name).where(Laptop.family_id.is_(None))  # type: ignore[union-attr]
    ).scalars().all()
    key_totals: dict[str, int] = {}
    for name in all_keys:
        key = family_key(name)
        key_totals[key] = key_totals.get(key, 0) + 1

    laptops = []
    for laptop, brand_name in rows:
        key = family_key(laptop.product_name)
        laptops.append(
            UnassignedLaptop(
                laptop_id=laptop.id,
                brand_id=laptop.brand_id,
                brand_name=brand_name,
                product_name=laptop.product_name,
                model_code=laptop.model_code,
                price_rm=laptop.price_rm,
                seed_key=key,
                seed_key_siblings=key_totals.get(key, 1) - 1,
            )
        )

    return UnassignedSummary(count=count, laptops=laptops)


@router.post(
    "/regroup",
    response_model=RegroupResult,
    dependencies=[Depends(get_current_admin)],
)
def regroup(session: Session = Depends(get_session)):
    """
    Auto-group the unassigned laptops. Runs over `family_id IS NULL` only, so
    it never moves a laptop a human has already placed — safe to re-run, and
    it cannot undo a merge.

    Same three counts as `python -m app.scripts.backfill_families --apply`;
    both call the same `family_service.regroup_unassigned`.
    """
    result = regroup_unassigned(session)
    logger.info(
        "families/regroup: %s created, %s assigned, %s left null",
        result["families_created"], result["laptops_assigned"], result["left_null"],
    )
    return RegroupResult(**result)


# --- Collection --------------------------------------------------------------

@router.get("", response_model=List[FamilyRead])
def list_families(
    response: Response,
    brand_id: Optional[uuid.UUID] = Query(default=None, description="Only families of this brand"),
    is_verified: Optional[bool] = Query(
        default=None, description="true, false, or omit for both"
    ),
    session: Session = Depends(get_session),
):
    """
    The admin's working view. Filtering on `is_verified=false` is the queue of
    groupings nobody has confirmed yet, which is what the auto-grouping leaves
    behind.

    `X-Unassigned-Count` carries the null-family backlog alongside the list, so
    the screen can show the number without a second round trip.
    """
    stmt = sa_select(LaptopFamily, LaptopBrand.name).join(
        LaptopBrand, LaptopBrand.id == LaptopFamily.brand_id  # type: ignore[arg-type]
    )
    if brand_id is not None:
        stmt = stmt.where(LaptopFamily.brand_id == brand_id)
    if is_verified is not None:
        stmt = stmt.where(LaptopFamily.is_verified == is_verified)
    # Family names tie across brands, so id closes the order — otherwise the
    # list reshuffles between requests and pagination would drop rows.
    stmt = stmt.order_by(LaptopBrand.name, LaptopFamily.name, LaptopFamily.id)

    counts = _member_counts(session)
    families = [
        _to_read(family, brand_name, counts.get(family.id, 0))
        for family, brand_name in session.execute(stmt).all()
    ]

    response.headers["X-Unassigned-Count"] = str(
        session.execute(
            sa_select(func.count(Laptop.id)).where(Laptop.family_id.is_(None))  # type: ignore[union-attr]
        ).scalar_one()
    )
    return families


@router.post(
    "",
    response_model=FamilyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_admin)],
)
def create_family(payload: FamilyCreate, session: Session = Depends(get_session)):
    """Create an empty family. The usual reason is a merge: make the family
    that will hold the product line, then move members into it."""
    brand = _assert_brand_exists(session, payload.brand_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Family name cannot be empty"
        )
    _assert_name_free(session, payload.brand_id, name)

    family = LaptopFamily(
        brand_id=payload.brand_id,
        name=name,
        family_key=payload.family_key,
        is_verified=payload.is_verified,
    )
    session.add(family)
    session.commit()
    session.refresh(family)
    return _to_read(family, brand.name, 0)


# --- Single family -----------------------------------------------------------

@router.get("/{family_id}", response_model=FamilyDetail)
def get_family(family_id: uuid.UUID, session: Session = Depends(get_session)):
    """The family and its member laptops."""
    family = _get_family(session, family_id)
    brand = session.get(LaptopBrand, family.brand_id)

    rows = session.execute(
        sa_select(Laptop)
        .where(Laptop.family_id == family_id)
        .order_by(Laptop.price_rm, Laptop.product_name, Laptop.id)
    ).scalars().all()

    members = [
        FamilyMember(
            laptop_id=laptop.id,
            product_name=laptop.product_name,
            model_code=laptop.model_code,
            price_rm=laptop.price_rm,
            status=laptop.status,
            # Shown per member because a family holding two different seed keys
            # is the signature of a completed merge — and of a mis-merge.
            seed_key=family_key(laptop.product_name),
        )
        for laptop in rows
    ]

    base = _to_read(family, brand.name if brand else "", len(members))
    return FamilyDetail(**base.model_dump(), laptops=members)


@router.patch(
    "/{family_id}",
    response_model=FamilyRead,
    dependencies=[Depends(get_current_admin)],
)
def update_family(
    family_id: uuid.UUID,
    payload: FamilyUpdate,
    session: Session = Depends(get_session),
):
    """Rename, move to another brand, or mark the grouping verified."""
    family = _get_family(session, family_id)
    data = payload.model_dump(exclude_unset=True)

    if "name" in data:
        if data["name"] is None or not data["name"].strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Family name cannot be empty",
            )
        data["name"] = data["name"].strip()
    if "brand_id" in data and data["brand_id"] is not None:
        _assert_brand_exists(session, data["brand_id"])

    target_brand = data.get("brand_id") or family.brand_id
    target_name = data.get("name") or family.name
    if "name" in data or "brand_id" in data:
        _assert_name_free(session, target_brand, target_name, exclude_id=family.id)

    for key, value in data.items():
        setattr(family, key, value)
    family.updated_at = datetime.now(timezone.utc)

    session.add(family)
    session.commit()
    session.refresh(family)

    brand = session.get(LaptopBrand, family.brand_id)
    return _to_read(family, brand.name if brand else "", _member_counts(session).get(family.id, 0))


@router.delete(
    "/{family_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_admin)],
)
def delete_family(family_id: uuid.UUID, session: Session = Depends(get_session)):
    """
    Delete a family and release its members.

    Members are set back to `family_id = NULL`, never deleted, and the delete
    is refused if that unassignment cannot happen — same shape as
    `laptop_router.delete_laptop`, which refuses rather than orphaning rows,
    because every FK to `laptops.id` in this schema is NO ACTION and nothing
    cleans up behind a cascade that does not exist.

    Releasing rather than blocking is the right default here (unlike a brand
    delete, which 409s on associated laptops): the second half of every merge
    is deleting an emptied family, and a null family_id is a safe state — it
    passes through dedup untouched and reappears in the regroup backlog.
    """
    family = _get_family(session, family_id)

    members = session.execute(
        sa_select(Laptop).where(Laptop.family_id == family_id)
    ).scalars().all()
    for laptop in members:
        laptop.family_id = None
        session.add(laptop)

    session.delete(family)
    session.commit()
    logger.info(
        "families: deleted '%s' (%s), released %s laptop(s)",
        family.name, family_id, len(members),
    )
    return None


# --- Membership --------------------------------------------------------------

@router.post(
    "/{family_id}/laptops",
    response_model=FamilyDetail,
    dependencies=[Depends(get_current_admin)],
)
def add_laptops(
    family_id: uuid.UUID,
    payload: FamilyLaptopsAssign,
    session: Session = Depends(get_session),
):
    """
    Move laptops into this family, whatever family they were in before. This
    is the first half of a merge; the second is deleting the emptied family.

    Unknown ids 404 before anything is written, so a typo cannot leave a merge
    half-applied — the same up-front validation the job endpoints use.
    """
    _get_family(session, family_id)

    if not payload.laptop_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No laptop ids given"
        )

    # Duplicate ids in one request are harmless (the same assignment twice),
    # so they are collapsed rather than rejected.
    wanted = list(dict.fromkeys(payload.laptop_ids))
    found = {
        laptop.id: laptop
        for laptop in session.execute(
            sa_select(Laptop).where(Laptop.id.in_(wanted))  # type: ignore[union-attr]
        ).scalars().all()
    }
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown laptop id(s): {', '.join(missing)}",
        )

    for laptop in found.values():
        laptop.family_id = family_id
        session.add(laptop)
    session.commit()

    return get_family(family_id, session)


@router.delete(
    "/{family_id}/laptops/{laptop_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_admin)],
)
def remove_laptop(
    family_id: uuid.UUID,
    laptop_id: uuid.UUID,
    session: Session = Depends(get_session),
):
    """Release one laptop back to unassigned. It stays in the catalog; it just
    stops being deduplicated against this family's other configurations."""
    _get_family(session, family_id)

    laptop = session.get(Laptop, laptop_id)
    if not laptop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found"
        )
    if laptop.family_id != family_id:
        # 404 rather than 204: silently succeeding would tell an admin their
        # merge worked when they had the wrong family open.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This laptop is not a member of that family",
        )

    laptop.family_id = None
    session.add(laptop)
    session.commit()
    return None
