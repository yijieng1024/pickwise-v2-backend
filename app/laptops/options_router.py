"""
Public configurator for Apple standard configs (step 10c, step (d)).

    GET /laptops/{id}/options     the chip kits of this laptop's product, size and line
    GET /laptops/{id}/configure   one configuration: total price and live Pick Score

404 for anything but an active Apple standard config with stored chip kits. A choice
the chosen chip does not allow is a 422 listing what the chip allows and which chips
offer the choice; the configurator never switches the chip by itself.

Education prices (step 10d), for the details page's Edu toggle and nothing else:

    GET /laptops/{id}/edu-price   the laptop at its Edu price with live Pick Scores,
                                  or `edu: null` when it has none (any brand)
    ?edu=true on the two above    the configurator priced from the education store;
                                  409 when its Edu numbers are not stored
"""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.laptops import options_service as svc
from app.laptops.laptop_models import Laptop
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
    edu: bool = False              # priced from the education store
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
    edu: bool = False              # priced from the education store
    add_ons: List[AddOn]
    total_rm: float
    scores: Optional[List[ConfiguredScore]]   # None when the chip has no benchmark
    score_unavailable_reason: Optional[str]
    # The caller's personalized score: only with a bearer token and a preference profile.
    personalized_score: Optional[Dict[str, Any]] = None


class EduPrice(BaseModel):
    price_rm: float
    official_price_rm: float
    saving_rm: float
    checked_at: datetime
    source: str
    source_url: Optional[str]
    scores: Optional[List[ConfiguredScore]]
    score_unavailable_reason: Optional[str]
    personalized_score: Optional[Dict[str, Any]] = None


class EduPriceResponse(BaseModel):
    laptop_id: uuid.UUID
    edu: Optional[EduPrice]        # None: no Edu price to show, so no toggle


_EDU = Query(False, description="Price from the education store (the details page's Edu toggle)")


def _configurable(session: Session, laptop_id: uuid.UUID, edu: bool = False) -> svc.Configurable:
    try:
        return svc.configurable(session, laptop_id, edu=edu)
    except svc.NotConfigurable:
        raise HTTPException(status_code=404, detail="No build-to-order options for this laptop")
    except svc.NoEduPrice:
        raise HTTPException(status_code=409, detail={
            "code": "no_edu_price", "message": "No education price is stored for this configuration"})


def _preference(session: Session, user: Optional[User]) -> Optional[LaptopUserPreference]:
    return session.exec(select(LaptopUserPreference).where(
        LaptopUserPreference.user_id == user.id)).first() if user else None


@router.get("/{laptop_id}/edu-price", response_model=EduPriceResponse)
def get_edu_price(laptop_id: uuid.UUID, session: Session = Depends(get_session),
                  user: Optional[User] = Depends(get_optional_user)):
    laptop = session.get(Laptop, laptop_id)
    if laptop is None:
        raise HTTPException(status_code=404, detail=f"Laptop {laptop_id} not found")
    return EduPriceResponse(laptop_id=laptop.id, edu=svc.edu_view(session, laptop, _preference(session, user)))


@router.get("/{laptop_id}/options", response_model=OptionsResponse)
def get_options(laptop_id: uuid.UUID, edu: bool = _EDU, session: Session = Depends(get_session)):
    c = _configurable(session, laptop_id, edu)
    return OptionsResponse(laptop_id=c.laptop.id, standard_chip=c.chip, standard=c.standard,
                           standard_price_rm=c.standard_price_rm, edu=c.edu, kits=svc.kits_listing(c))


@router.get("/{laptop_id}/configure", response_model=ConfigureResponse)
def configure(
    laptop_id: uuid.UUID,
    chip: Optional[str] = Query(None, description="Chip value, e.g. m5pro-18-20; default: the standard config's"),
    memory: Optional[str] = Query(None, description="e.g. 48gb"),
    storage: Optional[str] = Query(None, description="e.g. 2tb"),
    adapter: Optional[str] = Query(None, description="e.g. 96w"),
    display: Optional[str] = Query(None, description="standard or nano_texture"),
    keyboard: Optional[str] = Query(None, description="e.g. none (US English)"),
    edu: bool = _EDU,
    session: Session = Depends(get_session),
    user: Optional[User] = Depends(get_optional_user),
):
    c = _configurable(session, laptop_id, edu)
    given = {"memory": memory, "storage": storage, "adapter": adapter, "display": display, "keyboard": keyboard}
    try:
        chip, choices = svc.checked_choices(c, chip, given)
    except svc.NotAllowed as e:
        raise HTTPException(status_code=422, detail={"message": e.message, "not_allowed": e.problems})
    return svc.configure(session, c, chip, choices, user_pref=_preference(session, user))
