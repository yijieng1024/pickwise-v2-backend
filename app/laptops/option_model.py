"""
Apple build-to-order options, chip by chip (step 10c, decided 2026-11-04).

Apple's options are chip-driven: the chip decides which RAM and storage exist, and the
screen size only decides which chips exist. So the unit is a **chip kit**, keyed by
product + size + line (PROLED / Liquid Retina, "" for none) + chip, because the two
MacBook Pro lines price the same chip differently. Each kit has one `chip` row carrying
Apple's price for the kit (`kit_price_rm`) and one row per allowed option, priced
relative to that kit. A configuration's price is the kit's price plus its options'
add-ons (app/processor/apple_options.py::quote).

Filled from Apple's `cto/update-config` (app/scraper/apple_options_fetch.py). A laptop
finds its kits by its own product, size, line and chip; no FK, so the rows do not belong
to any one standard config (the four Air 15 standard configs share one kit).
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class LaptopOption(SQLModel, table=True):
    __tablename__ = "laptop_options"  # type: ignore
    __table_args__ = (
        UniqueConstraint("product", "size_inch", "line", "chip", "option_group", "value",
                         name="uq_laptop_options_kit_group_value"),
        Index("ix_laptop_options_kit", "product", "size_inch", "line", "chip"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    product: str = Field(max_length=40)            # "MacBook Air", "MacBook Pro"
    size_inch: int
    line: str = Field(default="", max_length=20)   # "proled", "liquid-retina", "" for none
    chip: str = Field(max_length=40)               # Apple's chip value: "10-8", "m5pro-15-16"
    option_group: str = Field(max_length=20)       # chip / memory / storage / adapter / display / keyboard
    value: str = Field(max_length=40)              # "24gb", "70w"; the chip itself on the chip row
    label: Optional[str] = None                    # what the page shows, e.g. "24GB"
    # Relative to the kit. Null: Apple prices no option of this group (keyboard), and on
    # the chip row, which carries the kit's own price instead.
    add_on_rm: Optional[float] = None
    kit_price_rm: Optional[float] = None           # chip row only: Apple's price for the kit
    is_included: bool = False                      # the kit comes with it
    is_blocked: bool = False                       # listed for the chip, not orderable
    cpu_model: Optional[str] = None                # chip row only, from the specs page
    gpu_model: Optional[str] = None
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
