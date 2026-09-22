"""Admin read/write for `app_settings`. One key today; the table is generic."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.common.app_settings import PROCESSOR_MODEL_KEY, get_setting, set_setting
from app.database import get_session
from app.processor.engine import (
    DEFAULT_EXTRACTION_MODEL,
    MODEL_OPTIONS,
    model_chain,
    resolve_extraction_model,
)
from app.users.auth import get_current_admin

router = APIRouter(
    prefix="/admin/settings",
    tags=["Admin - Settings"],
    dependencies=[Depends(get_current_admin)],
)


class ModelOption(BaseModel):
    """A selectable model WITH its free-tier budget — see MODEL_OPTIONS."""

    model: str
    tpm: int
    rpm: int
    # Requests per day. Advisory — nothing enforces it — but it is what says
    # whether a model can finish a full run at all.
    rpd: int


class ProcessorModelRead(BaseModel):
    model: str
    is_default: bool
    default_model: str
    # The chosen model first, then who covers for it when it is overloaded.
    fallback_chain: list[str]
    options: list[ModelOption]


class ProcessorModelUpdate(BaseModel):
    # None resets to the code default by deleting nothing — the row is simply
    # overwritten with the default, which keeps one code path.
    model: Optional[str] = None


def _read(session: Session) -> ProcessorModelRead:
    active = resolve_extraction_model(session)
    return ProcessorModelRead(
        model=active,
        is_default=get_setting(session, PROCESSOR_MODEL_KEY) is None,
        default_model=DEFAULT_EXTRACTION_MODEL,
        fallback_chain=list(model_chain(active)),
        options=[ModelOption(model=m, **limits) for m, limits in MODEL_OPTIONS.items()],
    )


@router.get("/processor-model", response_model=ProcessorModelRead)
def read_processor_model(session: Session = Depends(get_session)) -> ProcessorModelRead:
    """The model the AI processor will use on its next run, and the choices."""
    return _read(session)


@router.put("/processor-model", response_model=ProcessorModelRead)
def update_processor_model(
    payload: ProcessorModelUpdate,
    session: Session = Depends(get_session),
) -> ProcessorModelRead:
    """
    Switch the extraction model. Takes effect on the next record — the chain
    is built per call — so a running job finishes on the model it started with.

    Only allow-listed models are accepted: the rate limiter paces against the
    model's TPM/RPM budget, and a name with no budget attached cannot be paced.
    """
    chosen = payload.model or DEFAULT_EXTRACTION_MODEL
    if chosen not in MODEL_OPTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown model {chosen!r}. Choose one of: {', '.join(MODEL_OPTIONS)}.",
        )

    set_setting(session, PROCESSOR_MODEL_KEY, chosen)
    return _read(session)
