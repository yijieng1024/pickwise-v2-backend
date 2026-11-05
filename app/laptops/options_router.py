"""
Public configurator for Apple standard configs (step 10c, step (d)).

    GET /laptops/{id}/options     the chip kits of this laptop's product, size and line
    GET /laptops/{id}/configure   one configuration: total price and live Pick Score

404 for anything but an active Apple standard config with stored chip kits. A choice
the chosen chip does not allow is a 422 listing what the chip allows and which chips
offer the choice; the configurator never switches the chip by itself.
"""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.laptops import options_service as svc
from app.users.auth import get_optional_user
from app.users.models import LaptopUserPreference, User

router = APIRouter(prefix="/laptops", tags=["Apple options"])


class OptionValue(BaseModel):
    value: str
    label: Optional[str]
    add_on_rm: Optional[float]
    is_included: bool


class ChipKit(BaseModel):
    chip: str
    label: Optional[str]
    cpu_model: Optional[str]
    gpu_model: Optional[str]
    kit_price_rm: float
    chip_add_on_rm: float          # kit price - this laptop's standard price
    options: Dict[str, List[OptionValue]]


class OptionsResponse(BaseModel):
    laptop_id: uuid.UUID
    standard_chip: str
    standard: Dict[str, str]
    standard_price_rm: float
    kits: List[ChipKit]


class AddOn(BaseModel):
    group: str
    value: str
    label: Optional[str]
    add_on_rm: float


class ConfiguredScore(BaseModel):
    use_case: str
    score: Optional[int]
    breakdown: List[Dict[str, Any]]
    flags: Dict[str, Any]


class ConfigureResponse(BaseModel):
    laptop_id: uuid.UUID
    chip: str
    chip_label: Optional[str]
    cpu_model: Optional[str]
    gpu_model: Optional[str]
    standard_price_rm: float
    chip_add_on_rm: float
    add_ons: List[AddOn]
    total_rm: float
    scores: Optional[List[ConfiguredScore]]   # None when the chip has no benchmark
    score_unavailable_reason: Optional[str]
    # The caller's personalized score: only with a bearer token and a preference profile.
    personalized_score: Optional[Dict[str, Any]] = None


def _configurable(session: Session, laptop_id: uuid.UUID) -> svc.Configurable:
    try:
        return svc.configurable(session, laptop_id)
    except svc.NotConfigurable:
        raise HTTPException(status_code=404, detail="No build-to-order options for this laptop")


@router.get("/{laptop_id}/options", response_model=OptionsResponse)
def get_options(laptop_id: uuid.UUID, session: Session = Depends(get_session)):
    c = _configurable(session, laptop_id)
    return OptionsResponse(laptop_id=c.laptop.id, standard_chip=c.chip, standard=c.standard,
                           standard_price_rm=c.laptop.price_rm, kits=svc.kits_listing(c))


@router.get("/{laptop_id}/configure", response_model=ConfigureResponse)
def configure(
    laptop_id: uuid.UUID,
    chip: Optional[str] = Query(None, description="Chip value, e.g. m5pro-18-20; default: the standard config's"),
    memory: Optional[str] = Query(None, description="e.g. 48gb"),
    storage: Optional[str] = Query(None, description="e.g. 2tb"),
    adapter: Optional[str] = Query(None, description="e.g. 96w"),
    display: Optional[str] = Query(None, description="standard or nano_texture"),
    keyboard: Optional[str] = Query(None, description="e.g. none (US English)"),
    session: Session = Depends(get_session),
    user: Optional[User] = Depends(get_optional_user),
):
    c = _configurable(session, laptop_id)
    given = {"memory": memory, "storage": storage, "adapter": adapter, "display": display, "keyboard": keyboard}
    try:
        chip, choices = svc.checked_choices(c, chip, given)
    except svc.NotAllowed as e:
        raise HTTPException(status_code=422, detail={"message": e.message, "not_allowed": e.problems})
    pref = session.exec(select(LaptopUserPreference).where(
        LaptopUserPreference.user_id == user.id)).first() if user else None
    return svc.configure(session, c, chip, choices, user_pref=pref)
