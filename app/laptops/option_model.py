"""
Apple build-to-order options of a standard configuration (step 10c, decided 2026-11-04).

One `laptop_options` row per option Apple's configurator offers on a standard config,
priced relative to that standard config; one `laptop_option_rules` row per forced
change (24GB on the 13-inch Air forces the 10-core GPU chip). Both are filled from
Apple's `cto/update-config` and `cto/dependency-switcher` responses
(app/processor/apple_options.py) and are derived data, so a laptop's rows go with it
(ON DELETE CASCADE). No relationship on `Laptop`, which keeps this module importable
on its own.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LaptopOption(SQLModel, table=True):
    __tablename__ = "laptop_options"  # type: ignore
    __table_args__ = (
        UniqueConstraint("laptop_id", "option_group", "value", name="uq_laptop_options_laptop_group_value"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", ondelete="CASCADE", index=True)
    option_group: str = Field(max_length=20)       # chip / memory / storage / adapter / keyboard
    value: str = Field(max_length=40)              # Apple's variant value: "24gb", "10-10", "70w"
    label: Optional[str] = None                    # what the page shows, e.g. "24GB"
    # Relative to the standard config. Null: Apple prices no option of this group
    # (keyboard languages; the chip, which a rule or another standard config sets).
    add_on_rm: Optional[float] = None
    is_included: bool = False                      # the standard config comes with it
    is_blocked: bool = False                       # offered by the group, not on this config
    # Chip options only. The configurator gives just "10-core CPU, 10-core GPU", so these
    # come from the standard config with that chip, or the specs page for an upgrade-only chip.
    cpu_model: Optional[str] = None
    gpu_model: Optional[str] = None
    fetched_at: datetime = Field(default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))


class LaptopOptionRule(SQLModel, table=True):
    __tablename__ = "laptop_option_rules"  # type: ignore
    __table_args__ = (
        UniqueConstraint("laptop_id", "trigger_group", "trigger_value", "forced_group",
                         name="uq_laptop_option_rules_trigger_forced"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", ondelete="CASCADE", index=True)
    trigger_group: str = Field(max_length=20)
    trigger_value: str = Field(max_length=40)
    forced_group: str = Field(max_length=20)
    forced_value: str = Field(max_length=40)
    fetched_at: datetime = Field(default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
