import re
import uuid

from rapidfuzz import fuzz, process
from sqlalchemy import select as sa_select
from sqlmodel import Session

from app.laptops.brand_model import LaptopBrand
from app.laptops.customization_model import LaptopCustomization  # noqa: F401 — must precede Laptop to register with SQLAlchemy mapper
from app.laptops.laptop_models import Laptop

MATCH_THRESHOLD = 73.0

_STRIP_WORDS = re.compile(r'-?\b(inch|gb|tb|ram|ssd|hdd)\b', re.I)


def _build_match_key(brand_name: str, product_name: str) -> str:
    """
    Produce a compact match key that video titles are likely to contain.
    e.g. "Apple 14-inch MacBook Pro (M5, 16GB RAM, 1TB SSD)"
      -> "Apple 14 MacBook Pro M5"
    Strips "-inch", RAM/storage specs, and moves the chip out of parens.
    """
    chip_match = re.search(r'\(([^,)]+)', product_name)
    chip = chip_match.group(1).strip() if chip_match else ''

    base = re.sub(r'\([^)]*\)', '', product_name)   # drop parens block
    base = _STRIP_WORDS.sub('', base)               # drop -inch/GB/TB etc.
    base = re.sub(r'\s+', ' ', base).strip()

    parts = [brand_name, base]
    if chip and chip.lower() not in base.lower():
        parts.append(chip)
    return ' '.join(parts)


def match_laptop(
    video_title: str, session: Session
) -> tuple[uuid.UUID | None, float]:
    """
    Fuzzy-match a YouTube video title against the laptop catalog.
    Returns (laptop_id, confidence_score). laptop_id is None if below MATCH_THRESHOLD.
    Builds a compact match key per laptop so spec noise (RAM, storage) doesn't
    dilute the score — video titles rarely mention exact RAM or storage tiers.
    """
    rows = session.execute(
        sa_select(Laptop, LaptopBrand.name).join(
            LaptopBrand, LaptopBrand.id == Laptop.brand_id
        )
    ).all()

    if not rows:
        return None, 0.0

    candidates = [
        (_build_match_key(brand_name, laptop.product_name), laptop.id)
        for laptop, brand_name in rows
    ]
    names = [c[0] for c in candidates]

    result = process.extractOne(video_title, names, scorer=fuzz.token_set_ratio)
    if result is None:
        return None, 0.0

    _best_match, score, idx = result
    laptop_id = candidates[idx][1]

    if score >= MATCH_THRESHOLD:
        return laptop_id, float(score)
    return None, float(score)
