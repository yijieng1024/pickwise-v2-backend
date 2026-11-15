"""
Preview and apply one product guide. The preview and the apply run the same
`match` and the same fill rule, so what the admin sees is what gets written.

Fill rule: a guide price is written when the stored price is 0, or when the stored
price itself came from a guide. A page price, an admin's price and a price with no
recorded source are never overwritten by an apply; they are listed as differing from
the guide, and only an admin choosing "Use guide price" for one replaces it.

A match that holds only because -W and -WP read as one SKU, or only through the base
model code, is never written by an apply either: the preview lists it for
confirmation, and "Use guide price" writes it.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import (
    Laptop, LaptopPriceHistory, LaptopStatus, PriceGuideDecision, PriceGuideReview, PriceSource,
)
from app.laptops.price_guide import (
    CatalogRow, GuideRow, MatchResult, PricePoint, is_reviewed, match, panels_differ, review_key, row_warnings,
    sku_key,
)
from app.laptops.price_guide_model import PriceGuide, PriceGuideRow


class Action(str, Enum):
    FILL = "fill"  # the guide's price is written
    CONFIRM = "confirm"  # same price: Last Checked moves, nothing else
    DIFFERS = "differs"  # left alone and listed


class NotSelectable(Exception):
    """A selected laptop is on neither list "Use guide price" serves, so nothing was written."""

    def __init__(self, laptop_ids: list[uuid.UUID]):
        super().__init__(f"not waiting on this guide: {[str(i) for i in laptop_ids]}")
        self.laptop_ids = laptop_ids


class GuideInUse(Exception):
    """A price history row names this guide, so it is the record of where that price came from."""


def decide(stored_price: Optional[float], latest_source: Optional[str], guide_price: float) -> Action:
    if not stored_price:
        return Action.FILL
    if stored_price == guide_price:
        return Action.CONFIRM
    return Action.FILL if latest_source == PriceSource.GUIDE.value else Action.DIFFERS


def _catalog(session: Session) -> list[Laptop]:
    """ASUS laptops (ROG included), minus the rows that never need a price found:
    discontinued (no longer sold) and suspended (hidden), ADR-0009 Amendment II."""
    return list(session.exec(
        select(Laptop)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .where(func.lower(LaptopBrand.name) == "asus")
        .where(Laptop.status.notin_([LaptopStatus.SUSPENDED.value, LaptopStatus.DISCONTINUED.value]))  # type: ignore[attr-defined]
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
        [GuideRow(r.sku, r.model_code, r.price_rm, r.cpu, r.ram_gb, r.ssd_gb, r.page, r.panel) for r in rows],
        _catalog_rows(laptops),
    )


def _catalog_rows(laptops: list[Laptop]) -> list[CatalogRow]:
    return [
        CatalogRow(
            l.id, l.model_code, l.product_name, l.processor_model, l.ram_gb, l.ssd_gb, l.manufacturer_sku,
            l.display_type, family_id=l.family_id, gpu=l.gpu_model, price_rm=l.price_rm,
        )
        for l in laptops
    ]


def _histories(session: Session) -> dict[uuid.UUID, list[PricePoint]]:
    histories: dict[uuid.UUID, list[PricePoint]] = {}
    for laptop_id, recorded_at, price, source in session.exec(
        select(
            LaptopPriceHistory.laptop_id, LaptopPriceHistory.recorded_at,
            LaptopPriceHistory.price_rm, LaptopPriceHistory.source,
        ).order_by(LaptopPriceHistory.recorded_at)  # type: ignore[arg-type]
    ).all():
        histories.setdefault(laptop_id, []).append(PricePoint(recorded_at, price, source))
    return histories


def _reviews(session: Session) -> dict[tuple, PriceGuideReview]:
    """Every review, by review_key. A later review of the same key wins."""
    out: dict[tuple, PriceGuideReview] = {}
    for r in session.exec(select(PriceGuideReview).order_by(PriceGuideReview.reviewed_at)).all():  # type: ignore[arg-type]
        out[review_key(r.laptop_id, r.guide_sku, r.guide_price_rm)] = r
    return out


def _review(r: PriceGuideReview) -> dict:
    return {"decision": r.decision, "reviewed_at": r.reviewed_at.isoformat(),
            "reviewed_by": str(r.reviewed_by) if r.reviewed_by else None}


def _guide_row(row: GuideRow) -> dict:
    return {"sku": row.sku, "price_rm": row.price_rm, "page": row.page, "panel": row.panel}


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
    # _catalog leaves out suspended and discontinued rows, so neither can be the
    # other half of a duplicate: retiring a row is how a duplicate is fixed.
    catalog_rows = _catalog_rows(laptops)
    histories = _histories(session)

    claims = {
        c.id: rows
        for group in (result.matched, result.conflict, result.suffix_rule, result.base_code)
        for c, rows in group
    }

    def warn(row: CatalogRow, rows: list[GuideRow]) -> list[dict]:
        return row_warnings(row, rows, catalog_rows, histories.get(row.id, []), claims)

    reviews = _reviews(session)
    reviewed_out: list[dict] = []

    def keep(entry: dict, rows: list[GuideRow], listed_on: str) -> bool:
        """False when an admin has reviewed this laptop at this guide price: the entry goes
        to `reviewed` instead. Display and counts only; _match is untouched."""
        review = reviews.get(review_key(entry["id"], rows[0].sku, rows[0].price_rm))
        if review is None:
            return True
        reviewed_out.append({**entry, "listed_on": listed_on, "guide_sku": rows[0].sku,
                             "guide_price_rm": rows[0].price_rm, **_review(review)})
        return False

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
            "warnings": warn(catalog_row, rows),
        }
        if action != Action.DIFFERS:
            matched.append(entry)
        elif keep(entry, rows, "differs_from_guide"):
            differs.append(entry)

    suffix_rule = [
        e for c, rows in result.suffix_rule
        for e in [{
            **_laptop(by_id[c.id]),
            "catalog_sku": c.manufacturer_sku,
            "guide_sku": rows[0].sku,
            "guide_price_rm": rows[0].price_rm,
            "rows": [_guide_row(r) for r in rows],
            "warnings": warn(c, rows),
        }] if keep(e, rows, "matched_by_suffix_rule")
    ]
    base_code = [
        e for c, rows in result.base_code
        for e in [{
            **_laptop(by_id[c.id]),
            "guide_sku": rows[0].sku,
            "guide_price_rm": rows[0].price_rm,
            "rows": [_guide_row(r) for r in rows],
            "warnings": warn(c, rows),
        }] if keep(e, rows, "matched_by_base_code")
    ]

    return {
        "guide_id": str(guide.id),
        "counts": {
            "matched": len(matched),
            "differs_from_guide": len(differs),
            "matched_by_suffix_rule": len(suffix_rule),
            "matched_by_base_code": len(base_code),
            "reviewed": len(reviewed_out),
            "ambiguous": len(result.ambiguous),
            "conflict": len(result.conflict),
            "unmatched_guide": len(result.unmatched_guide),
            "unpriced": len(result.unpriced),
            "uncovered": len(result.uncovered),
        },
        "matched": matched,
        "differs_from_guide": differs,
        # -W read as -WP: an apply skips these, "Use guide price" writes them.
        "matched_by_suffix_rule": suffix_rule,
        # Found through the base code only: an apply skips these too.
        "matched_by_base_code": base_code,
        # Decided by an admin at this guide price; back on its list if the price changes.
        "reviewed": reviewed_out,
        # Priced here but not on sale: making them active is an admin's call.
        "priced_but_inactive": [m for m in matched if m["status"] == LaptopStatus.INACTIVE.value],
        "ambiguous": [
            {**_guide_row(row), "candidates": [_laptop(by_id[c.id]) for c in candidates]}
            for row, candidates in result.ambiguous
        ],
        "conflict": [
            {
                **_laptop(by_id[c.id]),
                "reason": "panel" if panels_differ(c, rows) else "price",
                "catalog_panel": c.panel,
                "rows": [_guide_row(r) for r in rows],
                "warnings": warn(c, rows),
            }
            for c, rows in result.conflict
        ],
        "unmatched_guide": [_guide_row(r) for r in result.unmatched_guide],
        "unpriced": [_guide_row(r) for r in result.unpriced],
        # No guide row, so only the duplicate and price history checks can speak.
        "uncovered": [{**_laptop(by_id[c.id]), "warnings": warn(c, [])} for c in result.uncovered],
    }


def _write_guide_price(session: Session, laptop: Laptop, price: float, guide_id: uuid.UUID, now: datetime) -> None:
    """The source on the history row is what lets a later guide update this price."""
    laptop.price_rm = price
    laptop.price_checked_at = now
    session.add(laptop)
    session.add(LaptopPriceHistory(
        laptop_id=laptop.id, price_rm=price, source=PriceSource.GUIDE.value, price_guide_id=guide_id,
    ))


def _selectable(session: Session, guide: PriceGuide) -> dict[uuid.UUID, tuple[Laptop, float, str]]:
    """laptop id -> (laptop, guide price, guide SKU) for every laptop on the differs,
    suffix-rule or base-code list, reviewed or not."""
    laptops = _catalog(session)
    by_id = {l.id: l for l in laptops}
    sources = _latest_sources(session)
    result = _match(session, guide, laptops)
    selectable = {c.id: (by_id[c.id], rows[0].price_rm, rows[0].sku) for c, rows in result.suffix_rule + result.base_code}
    for catalog_row, rows in result.matched:
        laptop = by_id[catalog_row.id]
        if decide(laptop.price_rm, sources.get(laptop.id), rows[0].price_rm) == Action.DIFFERS:
            selectable[laptop.id] = (laptop, rows[0].price_rm, rows[0].sku)
    return selectable


def _write_review(session: Session, guide: PriceGuide, laptop_id: uuid.UUID, sku: str, price: float,
                  decision: PriceGuideDecision, reviewed_by: Optional[uuid.UUID], now: datetime) -> None:
    """One review per laptop and guide: a second decision replaces the first."""
    row = session.exec(select(PriceGuideReview).where(
        PriceGuideReview.laptop_id == laptop_id, PriceGuideReview.guide_id == guide.id)).first()
    row = row or PriceGuideReview(laptop_id=laptop_id, guide_id=guide.id, guide_sku="", guide_price_rm=0, decision="")
    row.guide_sku, row.guide_price_rm, row.decision = sku_key(sku), price, decision.value
    row.reviewed_by, row.reviewed_at = reviewed_by, now
    session.add(row)


def mark_reviewed(session: Session, guide: PriceGuide, laptop_ids: set[uuid.UUID],
                  reviewed_by: Optional[uuid.UUID] = None,
                  decision: PriceGuideDecision = PriceGuideDecision.KEPT) -> dict:
    """"Mark reviewed" on the differs, suffix-rule or base-code list: the stored price stays
    and the row leaves the list and the email count until the guide price changes. All
    or nothing, like "Use guide price"."""
    selectable = _selectable(session, guide)
    not_listed = sorted(laptop_ids - selectable.keys(), key=str)
    if not_listed:
        raise NotSelectable(not_listed)
    now = datetime.now(timezone.utc)
    for laptop_id in laptop_ids:
        _, price, sku = selectable[laptop_id]
        _write_review(session, guide, laptop_id, sku, price, decision, reviewed_by, now)
    session.commit()
    return {"guide_id": str(guide.id), "reviewed": len(laptop_ids)}


def undo_reviewed(session: Session, guide: PriceGuide, laptop_ids: set[uuid.UUID]) -> dict:
    """Put the selected laptops back on their list: removes every review that hides them at
    this guide's price, an earlier guide's included (a review is carried across guides)."""
    selectable = _selectable(session, guide)
    removed = 0
    for review in session.exec(select(PriceGuideReview).where(PriceGuideReview.laptop_id.in_(laptop_ids))).all():  # type: ignore[attr-defined]
        found = selectable.get(review.laptop_id)
        if found and is_reviewed({review_key(review.laptop_id, review.guide_sku, review.guide_price_rm)},
                                 review.laptop_id, found[2], found[1]):
            session.delete(review)
            removed += 1
    session.commit()
    return {"guide_id": str(guide.id), "removed": removed}


def use_guide_price(session: Session, guide: PriceGuide, laptop_ids: set[uuid.UUID],
                    reviewed_by: Optional[uuid.UUID] = None) -> dict:
    """Replace the selected laptops' prices with this guide's, in one transaction.

    Only laptops on the differs, suffix-rule or base-code lists, worked out again here
    rather than trusted from the page: one that left since the preview refuses the
    whole selection. A confirmed price that is already right moves Last Checked only.
    """
    selectable = _selectable(session, guide)
    not_listed = sorted(laptop_ids - selectable.keys(), key=str)
    if not_listed:
        raise NotSelectable(not_listed)

    now = datetime.now(timezone.utc)
    for laptop_id in laptop_ids:
        laptop, price, sku = selectable[laptop_id]
        if laptop.price_rm == price:
            laptop.price_checked_at = now
            session.add(laptop)
        else:
            _write_guide_price(session, laptop, price, guide.id, now)
        # Decided: off the list until the guide price changes (Jack, 2026-11-15, option 3a).
        _write_review(session, guide, laptop_id, sku, price, PriceGuideDecision.USED_GUIDE, reviewed_by, now)
    session.commit()
    return {"guide_id": str(guide.id), "updated": len(laptop_ids)}


def _history_rows(session: Session, guide_id: uuid.UUID) -> int:
    return session.exec(
        select(func.count()).select_from(LaptopPriceHistory).where(LaptopPriceHistory.price_guide_id == guide_id)
    ).one()


def delete_guide(session: Session, guide: PriceGuide) -> None:
    """Remove a guide and its rows in one transaction, which frees its sha256 for a
    new extraction. A price taken from it blocks this: deleting undoes no price, and
    would only hide where the price came from. A confirm-only apply leaves no such
    row, so it does not block."""
    count = _history_rows(session, guide.id)
    if count:
        noun = "price" if count == 1 else "prices"
        raise GuideInUse(f"{count} {noun} came from this guide, so it cannot be deleted.")
    session.exec(delete(PriceGuideReview).where(PriceGuideReview.guide_id == guide.id))
    session.exec(delete(PriceGuideRow).where(PriceGuideRow.guide_id == guide.id))
    session.delete(guide)
    try:
        session.commit()
    except IntegrityError:
        # "Use guide price" wrote a history row after the count: the FK kept it.
        session.rollback()
        raise GuideInUse("A price was just written from this guide, so it cannot be deleted.")


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
    # Rows an admin decided on stay out of the counts the weekly email reports.
    reviewed = set(_reviews(session))

    def open_decision(catalog_row, rows) -> bool:
        return not is_reviewed(reviewed, catalog_row.id, rows[0].sku, rows[0].price_rm)

    filled = confirmed = differs = 0
    for catalog_row, rows in result.matched:
        laptop = by_id[catalog_row.id]
        price = rows[0].price_rm
        action = decide(laptop.price_rm, sources.get(laptop.id), price)
        if action == Action.FILL:
            _write_guide_price(session, laptop, price, guide.id, now)
            filled += 1
        elif action == Action.DIFFERS:
            differs += open_decision(catalog_row, rows)
        elif laptop.price_checked_at is None or laptop.price_checked_at < guide.uploaded_at:
            laptop.price_checked_at = now
            session.add(laptop)
            confirmed += 1

    session.commit()
    if progress is not None:
        # The job starts with the guide's row count as its estimate, but an apply
        # works through matched laptops, so a finished job read "16 of 97".
        done = max(len(result.matched), 1)
        progress.set_total(done)
        progress.advance(count=done)
    return {
        "guide_id": str(guide_id),
        "filled": filled,
        "confirmed": confirmed,
        "differs_from_guide": differs,
        "awaiting_confirmation": sum(open_decision(c, rows) for c, rows in result.suffix_rule + result.base_code),
        "skipped_unclear": len(result.ambiguous) + len(result.conflict),
    }
