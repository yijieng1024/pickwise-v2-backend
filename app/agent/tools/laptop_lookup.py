"""
lookup_laptop: find a laptop the user names (10b step c, decided 2026-11-04).

Searches active and discontinued rows (ADR-0009 Amendment II): a discontinued machine
is found by name and reported as no longer sold, with its last official price. Never
inactive or suspended.

Scoring is the catalog layer's (`market_price._catalog_lookup`: token_set_ratio over the
name and "brand name", floor 75), copied rather than shared so the market price tool is
untouched. What differs is what happens after scoring:

- A year in the query ("2023") is removed before scoring and used only to prefer rows of
  that year among the close ones. Left in, it pushed "TUF A16 2023" below the floor.
- A standalone screen size (13, 14, 15, 16, 18) prefers rows of that size among the
  close ones: token_set_ratio does not tell "MacBook Pro 14" from a 16-inch Pro.
- Close = within 10 points of the top score. The close rows are grouped by family. One
  family is a single answer with its configurations; several are candidates, one per
  family, and the tool never picks one by itself.
"""
import re
from typing import Iterable, Mapping, Optional

from langchain_core.tools import tool
from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlmodel import Session

from app.database import engine
from app.laptops.brand_model import LaptopBrand
from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop, LaptopPriceHistory, LaptopStatus

FLOOR = 75  # the catalog layer's _CATALOG_THRESHOLD
CLOSE = 10
_YEAR = re.compile(r"\b(20\d{2})\b")
_SIZE = re.compile(r"(?<![\d.])(13|14|15|16|18)(?![\d.])")
_FOUND = (LaptopStatus.ACTIVE.value, LaptopStatus.DISCONTINUED.value)
_MAX_CONFIGS = 15


def _score(query: str, row: Mapping) -> float:
    name = (row["product_name"] or "").lower()
    score = fuzz.token_set_ratio(query, name)
    if row.get("brand"):
        score = max(score, fuzz.token_set_ratio(query, f"{row['brand'].lower()} {name}"))
    return score


def _prefer(rows: list[dict], keep) -> list[dict]:
    """A tie-breaker: the rows *keep* accepts, or all of them when it accepts none."""
    kept = [r for r in rows if keep(r)]
    return kept or rows


def _config(row: Mapping) -> dict:
    out = {"laptop_id": str(row["id"]), "product_name": row["product_name"], "status": row["status"]}
    if row["status"] == LaptopStatus.DISCONTINUED.value:
        out["no_longer_sold"] = True
        out["last_official_price_rm"] = row.get("last_price_rm")
        out["last_official_price_date"] = row.get("last_price_date")
    else:
        out["price_rm"] = row["price_rm"]
    return out


def rank(query: str, rows: Iterable[Mapping]) -> dict:
    """Pure. rows: id, product_name, brand, status, family_id, family_name,
    display_size_inch, release_year, price_rm, last_price_rm, last_price_date."""
    q = query.lower()
    year = _YEAR.search(q)
    q = " ".join(_YEAR.sub(" ", q).split())
    size = _SIZE.search(q)
    scored = [(_score(q, r), dict(r)) for r in rows if r["status"] in _FOUND]
    scored = [(s, r) for s, r in scored if s >= FLOOR]
    if not scored:
        return {"match": "none"}
    top = max(s for s, _ in scored)
    close = [r for s, r in sorted(scored, key=lambda t: (-t[0], t[1]["product_name"] or "")) if s >= top - CLOSE]
    if size:
        close = _prefer(close, lambda r: int(r["display_size_inch"] or 0) == int(size.group(1)))
    if year:
        y = int(year.group(1))
        close = _prefer(close, lambda r: r.get("release_year") == y or year.group(1) in (r["product_name"] or ""))

    families: dict = {}
    for r in close:
        families.setdefault(r["family_id"] or r["id"], []).append(r)
    if len(families) == 1:
        [members] = families.values()
        return {"match": "single", "family": members[0]["family_name"] or members[0]["product_name"],
                "configurations": [_config(r) for r in members[:_MAX_CONFIGS]],
                "more_configurations": max(0, len(members) - _MAX_CONFIGS)}
    return {"match": "candidates", "candidates": [
        {"family": m[0]["family_name"] or m[0]["product_name"], "example": m[0]["product_name"],
         "configurations": len(m), "statuses": sorted({r["status"] for r in m})}
        for m in families.values()
    ]}


def read_rows(session: Session) -> list[dict]:
    """Active and discontinued laptops with brand, family and last price."""
    latest = (select(LaptopPriceHistory.laptop_id, func.max(LaptopPriceHistory.recorded_at).label("at"))
              .group_by(LaptopPriceHistory.laptop_id).subquery())
    stmt = (
        select(Laptop.id, Laptop.product_name, LaptopBrand.name.label("brand"), Laptop.status, Laptop.family_id,
               LaptopFamily.name.label("family_name"), Laptop.display_size_inch, Laptop.release_year,
               Laptop.price_rm, LaptopPriceHistory.price_rm.label("last_price_rm"), latest.c.at)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .outerjoin(LaptopFamily, LaptopFamily.id == Laptop.family_id)
        .outerjoin(latest, latest.c.laptop_id == Laptop.id)
        .outerjoin(LaptopPriceHistory, (LaptopPriceHistory.laptop_id == Laptop.id)
                   & (LaptopPriceHistory.recorded_at == latest.c.at))
        .where(Laptop.status.in_(_FOUND))  # type: ignore[attr-defined]
    )
    rows = []
    for r in session.execute(stmt).mappings():
        row = dict(r)
        at = row.pop("at")
        row["last_price_date"] = at.date().isoformat() if at else None
        rows.append(row)
    return rows


@tool
def lookup_laptop(name: str) -> dict:
    """Find a laptop the user names, e.g. "MacBook Air M5" or "TUF A16 2023".
    Returns match "single" (one product line, with its configurations and status),
    "candidates" (several product lines match: ask the user which one, never choose),
    or "none". A configuration with no_longer_sold is discontinued: say it is no longer
    sold, give its last official price and date, and do not recommend it."""
    with Session(engine) as session:
        return rank(name, read_rows(session))
