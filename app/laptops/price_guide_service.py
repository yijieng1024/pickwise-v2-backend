"""
Preview and apply one product guide. The preview and the apply run the same
`match` and the same fill rule, so what the admin sees is what gets written.

Fill rule: a guide price is written when the stored price is 0, or when the stored
price itself came from a guide. A page price, an admin's price and a price with no
recorded source are never overwritten; they are listed as differing from the guide.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopPriceHistory, LaptopStatus, PriceSource
from app.laptops.price_guide import CatalogRow, GuideRow, MatchResult, match
from app.laptops.price_guide_model import PriceGuide, PriceGuideRow


class Action(str, Enum):
    FILL = "fill"  # the guide's price is written
    CONFIRM = "confirm"  # same price: Last Checked moves, nothing else
    DIFFERS = "differs"  # left alone and listed


def decide(stored_price: Optional[float], latest_source: Optional[str], guide_price: float) -> Action:
    if not stored_price:
        return Action.FILL
    if stored_price == guide_price:
        return Action.CONFIRM
    return Action.FILL if latest_source == PriceSource.GUIDE.value else Action.DIFFERS


def _catalog(session: Session) -> list[Laptop]:
    """ASUS laptops (ROG included), minus the retired archive: a discontinued
    machine does not need a price."""
    return list(session.exec(
        select(Laptop)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .where(func.lower(LaptopBrand.name) == "asus")
        .where(Laptop.status != LaptopStatus.SUSPENDED.value)
        .order_by(Laptop.id)
    ).all())


def _latest_sources(session: Session) -> dict[uuid.UUID, Optional[str]]:
    rows = session.exec(
        select(LaptopPriceHistory.laptop_id, LaptopPriceHistory.source)
        .distinct(LaptopPriceHistory.laptop_id)
        .order_by(LaptopPriceHistory.laptop_id, LaptopPriceHistory.recorded_at.desc())  # type: ignore[attr-defined]
    ).all()
    return dict(rows)


def _match(session: Session, guide: PriceGuide, laptops: list[Laptop]) -> MatchResult:
    rows = session.exec(
        select(PriceGuideRow).where(PriceGuideRow.guide_id == guide.id).order_by(PriceGuideRow.page, PriceGuideRow.sku)
    ).all()
    return match(
        [GuideRow(r.sku, r.model_code, r.price_rm, r.cpu, r.ram_gb, r.ssd_gb, r.page) for r in rows],
        [
            CatalogRow(l.id, l.model_code, l.product_name, l.processor_model, l.ram_gb, l.ssd_gb, l.manufacturer_sku)
            for l in laptops
        ],
    )


def _guide_row(row: GuideRow) -> dict:
    return {"sku": row.sku, "price_rm": row.price_rm, "page": row.page}


def _laptop(laptop: Laptop) -> dict:
    return {
        "id": str(laptop.id),
        "product_name": laptop.product_name,
        "model_code": laptop.model_code,
        "status": laptop.status,
        "price_rm": laptop.price_rm,
    }


def preview(session: Session, guide: PriceGuide) -> dict:
    """What an apply would do, and everything it would leave for a human. Reads only."""
    laptops = _catalog(session)
    by_id = {l.id: l for l in laptops}
    result = _match(session, guide, laptops)
    sources = _latest_sources(session)

    matched, differs = [], []
    for catalog_row, rows in result.matched:
        laptop = by_id[catalog_row.id]
        price = rows[0].price_rm
        action = decide(laptop.price_rm, sources.get(laptop.id), price)
        entry = {
            **_laptop(laptop),
            "guide_price_rm": price,
            "action": action.value,
            "price_source": sources.get(laptop.id),
            "rows": [_guide_row(r) for r in rows],
        }
        (differs if action == Action.DIFFERS else matched).append(entry)

    return {
        "guide_id": str(guide.id),
        "counts": {
            "matched": len(matched),
            "differs_from_guide": len(differs),
            "ambiguous": len(result.ambiguous),
            "conflict": len(result.conflict),
            "unmatched_guide": len(result.unmatched_guide),
            "unpriced": len(result.unpriced),
            "uncovered": len(result.uncovered),
        },
        "matched": matched,
        "differs_from_guide": differs,
        # Priced here but not on sale: making them active is an admin's call.
        "priced_but_inactive": [m for m in matched if m["status"] == LaptopStatus.INACTIVE.value],
        "ambiguous": [
            {**_guide_row(row), "candidates": [_laptop(by_id[c.id]) for c in candidates]}
            for row, candidates in result.ambiguous
        ],
        "conflict": [
            {**_laptop(by_id[c.id]), "rows": [_guide_row(r) for r in rows]} for c, rows in result.conflict
        ],
        "unmatched_guide": [_guide_row(r) for r in result.unmatched_guide],
        "unpriced": [_guide_row(r) for r in result.unpriced],
        "uncovered": [_laptop(by_id[c.id]) for c in result.uncovered],
    }


def apply_guide(session: Session, guide_id: uuid.UUID, progress=None) -> dict:
    """Write every `matched` row in one transaction, so a failure writes nothing.

    Writes go through the ORM like PUT /laptops/{id}: the mapper event and the
    updated_at stamp run as usual, and a price restored here does not re-activate
    a laptop. Re-applying changes nothing: the second run finds the same price, and
    its Last Checked already newer than the guide.
    """
    guide = session.get(PriceGuide, guide_id)
    if guide is None:
        raise ValueError(f"price guide {guide_id} not found")

    laptops = _catalog(session)
    by_id = {l.id: l for l in laptops}
    result = _match(session, guide, laptops)
    sources = _latest_sources(session)
    now = datetime.now(timezone.utc)

    filled = confirmed = differs = 0
    for catalog_row, rows in result.matched:
        laptop = by_id[catalog_row.id]
        price = rows[0].price_rm
        action = decide(laptop.price_rm, sources.get(laptop.id), price)
        if action == Action.FILL:
            laptop.price_rm = price
            laptop.price_checked_at = now
            session.add(laptop)
            session.add(LaptopPriceHistory(
                laptop_id=laptop.id, price_rm=price, source=PriceSource.GUIDE.value, price_guide_id=guide.id,
            ))
            filled += 1
        elif action == Action.DIFFERS:
            differs += 1
        elif laptop.price_checked_at is None or laptop.price_checked_at < guide.uploaded_at:
            laptop.price_checked_at = now
            session.add(laptop)
            confirmed += 1

    session.commit()
    if progress is not None:
        progress.advance(count=max(len(result.matched), 1))
    return {
        "guide_id": str(guide_id),
        "filled": filled,
        "confirmed": confirmed,
        "differs_from_guide": differs,
        "skipped_unclear": len(result.ambiguous) + len(result.conflict),
    }
