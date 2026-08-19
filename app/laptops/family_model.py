"""
The `laptop_family` table: the human-correctable product line a laptop belongs to.

A family is a COARSE product line, at the granularity a manufacturer segments
its own catalog by -- not a configuration, not a chassis code, not a model
year. Apple has three (MacBook Air, MacBook Pro, MacBook Neo: Air 13 and Air
15 are one family, and so are Pro 14 and Pro 16, because the chip tiers
overlap between Air and Pro so size is not a product boundary). Every ROG
Strix is one family -- Strix G16, SCAR 16 and SCAR 18 together.

`app.laptops.family_key.family_key` proposes a starting grouping from the
product name, and that seed is FINER than this taxonomy wants. Merging up to
the product line is a human action, taken through the /families CRUD, which
is why two schema decisions here are load-bearing:

  - `family_key` is NOT unique. Several seed keys legitimately map to one
    family ("14-inch macbook pro" and "16-inch macbook pro" are both MacBook
    Pro), and a unique constraint would make that merge impossible to express.
  - `family_key` is therefore PROVENANCE, not a lookup key. It records the
    seed the family was auto-created from and goes stale the moment a merge
    folds a second seed into it. Nothing may resolve a family by matching on
    it -- see family_service.resolve_family_id for the rule that replaces it.

No SQLModel Relationship to Laptop, deliberately. Members are read with an
explicit join (family_service), which keeps this module importable on its own
with no string-name resolution to configure -- the failure mode described
under "Circular import resolution" in CLAUDE.md. laptop_models.py imports this
module at the bottom so `laptops.family_id`'s foreign key always has its
target table in the metadata.
"""
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Field, SQLModel


class LaptopFamily(SQLModel, table=True):
    __tablename__ = "laptop_family"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    brand_id: uuid.UUID = Field(foreign_key="laptop_brands.id", index=True)
    name: str = Field(index=True)
    # Indexed but NOT unique -- see the module docstring. Nullable because a
    # hand-created family (the target of a merge) was never seeded from a key.
    family_key: Optional[str] = Field(default=None, index=True)
    # Plain boolean: "a human has looked at this grouping and it is right".
    # Auto-created families start false; that is the admin's work queue.
    is_verified: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- API schemas -------------------------------------------------------------

class FamilyCreate(SQLModel):
    brand_id: uuid.UUID
    name: str
    family_key: Optional[str] = None
    is_verified: bool = False


class FamilyUpdate(SQLModel):
    """PATCH semantics: every field optional, `exclude_unset` decides what is
    written, so omitting a field leaves it alone and sending null on the
    nullable ones clears them."""
    brand_id: Optional[uuid.UUID] = None
    name: Optional[str] = None
    family_key: Optional[str] = None
    is_verified: Optional[bool] = None


class FamilyRead(SQLModel):
    id: uuid.UUID
    brand_id: uuid.UUID
    brand_name: str
    name: str
    family_key: Optional[str]
    is_verified: bool
    member_count: int
    created_at: datetime
    updated_at: datetime


class FamilyMember(SQLModel):
    """A member laptop, trimmed to what the admin family screens render. The
    full spec record is a `LaptopRead` from /laptops/{id}; repeating 200 fields
    per member would make a 14-config family a heavy response for a table that
    shows four columns."""
    laptop_id: uuid.UUID
    product_name: str
    model_code: str
    price_rm: float
    status: str
    seed_key: str


class FamilyDetail(FamilyRead):
    laptops: List[FamilyMember] = Field(default_factory=list)


class FamilyLaptopsAssign(SQLModel):
    """Body of POST /families/{id}/laptops. Plural because a merge moves a
    whole family's worth of members at once, and doing that one request per
    laptop would leave a half-merged family on any failure."""
    laptop_ids: List[uuid.UUID]


class FamilyLaptopsMove(SQLModel):
    """Body of POST /families/laptops/move — the same bulk move as
    POST /families/{id}/laptops, but with the destination in the body so one
    call can also RELEASE the selection (`target_family_id: null`).

    That is what makes it usable from the screens where the destination is not
    the page you are on: the unassigned backlog, and a family detail view
    whose "remove selected" is a move to nowhere.

    `target_family_id` is nullable but REQUIRED — no default. Releasing every
    selected laptop is not something a caller should be able to do by
    forgetting a field; sending `null` has to be a decision."""
    laptop_ids: List[uuid.UUID]
    target_family_id: Optional[uuid.UUID]


class EmptiedFamily(SQLModel):
    """A family the move left with zero members. Reported, not deleted —
    deleting it is the admin's explicit second half of a merge, and an empty
    family is a legitimate state (POST /families creates one)."""
    family_id: uuid.UUID
    name: str


class LaptopsMoveResult(SQLModel):
    target_family_id: Optional[uuid.UUID]
    target_family_name: Optional[str]
    # Laptops whose family_id actually changed, vs. those already in the
    # target. Re-sending a selection is a no-op, so `moved: 0` is a success.
    moved: int
    unchanged: int
    emptied_families: List[EmptiedFamily] = Field(default_factory=list)
    # The destination after the move, so the screen can repaint from one
    # response. Null when the laptops were released to unassigned.
    target: Optional[FamilyDetail] = None


class UnassignedLaptop(SQLModel):
    laptop_id: uuid.UUID
    brand_id: uuid.UUID
    brand_name: str
    product_name: str
    model_code: str
    price_rm: float
    seed_key: str
    # How many OTHER unassigned laptops share this seed key. A high number is
    # a family waiting to be created; 1 is a one-config machine or a name the
    # seed key split off on its own.
    seed_key_siblings: int


class UnassignedSummary(SQLModel):
    count: int
    laptops: List[UnassignedLaptop] = Field(default_factory=list)


class RegroupResult(SQLModel):
    """The counts app/scripts/backfill_families.py prints, returned over HTTP
    so the admin screen and the script report the same three numbers."""
    families_created: int
    laptops_assigned: int
    left_null: int
