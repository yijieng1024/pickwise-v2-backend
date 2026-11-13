"""
Settings of the weekly pipeline (plan step 11, item 8), edited by the admin.

One row, id 1, created with the defaults by migration a2d6f8c4e1b9. Secrets (the
database URL, API keys) are never here: they stay in GitHub secrets, which the
workflow reads. Apple and Acer are never part of the pipeline, so they get no switch.

The switches are per site, not per `laptop_brands` row: ASUS and ROG are one brand
(`asus`) whose pages live on two sites, and HP is the third.
"""
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, EmailStr, Field as PydanticField, field_validator
from sqlalchemy import Column, DateTime
from sqlmodel import Field, Session, SQLModel

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Switch -> (brand row it belongs to, the site its pages are on)
SITES = {
    "asus": ("asus", "www.asus.com"),
    "rog": ("asus", "rog.asus.com"),
    "hp": ("hp", "www.hp.com"),
}


class PipelineSettings(SQLModel, table=True):
    __tablename__ = "pipeline_settings"  # type: ignore

    id: int = Field(default=1, primary_key=True)
    enabled: bool = False
    # Python's weekday names, in Malaysia time.
    weekday: str = Field(default="sunday", max_length=9)
    # Empty: no email is sent, and the run result says so.
    report_email: str = Field(default="", max_length=254)
    price_jump_percent: int = 30
    asus_enabled: bool = True
    rog_enabled: bool = True
    hp_enabled: bool = True
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), sa_column=Column(DateTime(timezone=True), nullable=False)
    )

    def sites(self) -> dict[str, list[str]]:
        """Brand name -> the sites switched on for it. A brand with none is not crawled."""
        on: dict[str, list[str]] = {}
        for switch, (brand, host) in SITES.items():
            if getattr(self, f"{switch}_enabled"):
                on.setdefault(brand, []).append(host)
        return on


class PipelineSettingsRead(BaseModel):
    enabled: bool
    weekday: str
    report_email: str
    price_jump_percent: int
    asus_enabled: bool
    rog_enabled: bool
    hp_enabled: bool
    updated_at: datetime


class PipelineSettingsUpdate(BaseModel):
    """Omit a field to leave it unchanged. `report_email: ""` turns the email off."""

    enabled: Optional[bool] = None
    weekday: Optional[str] = None
    report_email: Optional[str] = None
    price_jump_percent: Optional[int] = PydanticField(default=None, ge=1, le=100)
    asus_enabled: Optional[bool] = None
    rog_enabled: Optional[bool] = None
    hp_enabled: Optional[bool] = None

    @field_validator("weekday")
    @classmethod
    def _weekday(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().lower()
        if value not in WEEKDAYS:
            raise ValueError(f"weekday must be one of {', '.join(WEEKDAYS)}")
        return value

    @field_validator("report_email")
    @classmethod
    def _email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if value:
            _Email(address=value)  # raises a ValueError naming what is wrong
        return value


class _Email(BaseModel):
    address: EmailStr


def read_settings(session: Session) -> PipelineSettings:
    """The row, or the defaults if it is missing (a database before the migration's row)."""
    return session.get(PipelineSettings, 1) or PipelineSettings()


def update_settings(session: Session, payload: PipelineSettingsUpdate) -> PipelineSettings:
    row = session.get(PipelineSettings, 1) or PipelineSettings()
    for name in payload.model_fields_set:
        value = getattr(payload, name)
        if value is not None:
            setattr(row, name, value)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
