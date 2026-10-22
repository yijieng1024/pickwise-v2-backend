"""Admin read/write for `app_settings`: chat models per use (primary + ordered backups).

`/llm*` is the general surface (every use). `/processor-model` predates it and
is kept, same shape, because the admin processing page calls it; it reads and
writes the same `processor.model` row the general surface does.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session

from app.common import llm_config
from app.common.app_settings import delete_setting, set_setting
from app.common.llm_config import MODEL_OPTIONS
from app.database import get_session
from app.logger import get_logger
from app.processor.engine import DEFAULT_EXTRACTION_MODEL
from app.users.auth import get_current_admin

logger = get_logger(__name__)

router = APIRouter(
    prefix="/admin/settings",
    tags=["Admin - Settings"],
    dependencies=[Depends(get_current_admin)],
)


# --------------------------------------------------------------------------
# /llm -- every use
# --------------------------------------------------------------------------
class ModelSlotRead(BaseModel):
    # The spec in effect, e.g. "google_genai:gemma-4-31b-it", and its bare id.
    value: str
    model: str
    # "app_settings" (an admin choice is in effect) or "env" (the default is).
    source: str
    env_default: str
    # The raw stored row, if any. With `error` set it was REJECTED and the env
    # default is in effect instead -- show both so the admin can fix it.
    stored: Optional[str] = None
    error: Optional[str] = None
    # False = not in Gemini's models.list (retired or a typo). None = the list
    # could not be fetched, so nothing is known either way.
    in_model_list: Optional[bool] = None


class BackupSlotRead(BaseModel):
    # 1-based: position 1 is tried first after the primary.
    position: int
    value: str
    model: str
    in_model_list: Optional[bool] = None


class LLMUseRead(BaseModel):
    use: str
    primary: ModelSlotRead
    # Tried in this order when the model before it 429s, times out or 5xxs.
    backups: list[BackupSlotRead]
    # Where the list came from and, as for the primary, the raw stored row and
    # why it was rejected. A stored list is all-or-nothing: if any entry is
    # invalid, the whole env default list is in effect.
    backups_source: str
    backups_env_default: list[str]
    backups_stored: Optional[str] = None
    backups_error: Optional[str] = None
    # None for the uncapped background uses.
    max_backups: Optional[int] = None
    fallback_enabled: bool


class LLMSettingsRead(BaseModel):
    uses: list[LLMUseRead]
    # Human-readable problems: rejected rows, models missing from models.list.
    warnings: list[str]
    model_list_error: Optional[str] = None


class LLMUseUpdate(BaseModel):
    # Omit a field to leave it unchanged; null resets it to the env default;
    # `backups: []` means no fallback for this use.
    primary: Optional[str] = None
    backups: Optional[list[str]] = None


class AvailableModel(BaseModel):
    name: str
    spec: str  # what to send to PUT /llm/{use}
    display_name: str
    input_token_limit: Optional[int] = None
    output_token_limit: Optional[int] = None
    thinking: Optional[bool] = None
    # Where this model is configured, e.g. "agent.primary", "agent.backup[1]".
    configured_for: list[str] = []


class AvailableModelsRead(BaseModel):
    models: list[AvailableModel]
    fetched_at: datetime
    # Configured specs that are NOT in the list -- retired, or never existed.
    configured_missing: list[str]
    note: str


def _available_ids(refresh: bool = False) -> tuple[Optional[set[str]], Optional[str]]:
    try:
        models, _ = llm_config.model_list(refresh=refresh)
        return {m["name"] for m in models}, None
    except Exception as e:
        logger.warning("Gemini models.list failed: %s", e)
        return None, f"{type(e).__name__}: {e}"


def _in_list(spec: str, available: Optional[set[str]]) -> Optional[bool]:
    return None if available is None else llm_config.model_id(spec) in available


def _roles(r: llm_config.UseModels) -> list[tuple[str, str]]:
    """(role label, spec) for every model in the chain, in the order tried."""
    return [("primary", r.primary.value)] + [
        (f"backup[{i}]", spec) for i, spec in enumerate(r.backups.values, start=1)
    ]


def _use_read(use: str, available: Optional[set[str]]) -> LLMUseRead:
    r = llm_config.resolve(use)
    p = r.primary
    return LLMUseRead(
        use=use,
        primary=ModelSlotRead(
            value=p.value, model=llm_config.model_id(p.value), source=p.source,
            env_default=p.env_default, stored=p.stored, error=p.error,
            in_model_list=_in_list(p.value, available),
        ),
        backups=[
            BackupSlotRead(position=i, value=spec, model=llm_config.model_id(spec),
                           in_model_list=_in_list(spec, available))
            for i, spec in enumerate(r.backups.values, start=1)
        ],
        backups_source=r.backups.source,
        backups_env_default=list(r.backups.env_default),
        backups_stored=r.backups.stored,
        backups_error=r.backups.error,
        max_backups=llm_config.MAX_BACKUPS.get(use),
        fallback_enabled=bool(r.backups.values),
    )


@router.get("/llm", response_model=LLMSettingsRead)
def read_llm_settings() -> LLMSettingsRead:
    """Primary + ordered backups for every use, where each value came from,
    and anything wrong with them."""
    available, list_error = _available_ids()
    uses = [_use_read(use, available) for use in llm_config.USES]
    warnings = []
    for u in uses:
        if u.primary.error:
            warnings.append(
                f"{u.use} primary: stored value {u.primary.stored!r} is invalid ({u.primary.error}); "
                f"using env default {u.primary.env_default}."
            )
        if u.backups_error:
            warnings.append(
                f"{u.use} backups: stored list {u.backups_stored} is invalid ({u.backups_error}); "
                f"using env default {u.backups_env_default}."
            )
        roles = [("primary", u.primary.model, u.primary.in_model_list)] + [
            (f"backup[{b.position}]", b.model, b.in_model_list) for b in u.backups
        ]
        for role, model, listed in roles:
            if listed is False:
                warnings.append(f"{u.use} {role}: {model} is not in Gemini's models.list (retired?).")
    return LLMSettingsRead(uses=uses, warnings=warnings, model_list_error=list_error)


def _bad(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, msg)


def _apply_update(
    session: Session, use: str, payload: LLMUseUpdate, check_model_list: bool = True,
) -> Optional[set[str]]:
    """Validate the whole change against the chain it produces, then write.
    Returns the models.list ids it checked against (None = not fetched)."""
    current = llm_config.resolve(use)
    fields = payload.model_fields_set

    primary = current.primary.value
    if "primary" in fields:
        if payload.primary is None:
            primary = llm_config.env_default(use)
        else:
            try:
                primary = llm_config.validate_spec(use, payload.primary)
            except ValueError as e:
                raise _bad(f"primary: {e}")

    backups: Optional[tuple[str, ...]] = None  # None = no stored list after this
    if "backups" in fields:
        if payload.backups is not None:
            try:
                backups = llm_config.validate_backups(use, payload.backups, primary)
            except ValueError as e:
                raise _bad(str(e))
    elif current.backups.source == "app_settings":
        # The stored list stays, so it must still be valid under the new primary.
        try:
            backups = llm_config.validate_backups(use, list(current.backups.values), primary)
        except ValueError as e:
            raise _bad(f"{e} Send `backups` in the same request to change the list too.")

    available = None
    if check_model_list:
        new_specs = []
        if "primary" in fields and payload.primary is not None:
            new_specs.append(("primary", primary))
        if "backups" in fields and backups:
            new_specs += [(f"backups[{i}]", b) for i, b in enumerate(backups)]
        if new_specs:
            available, _ = _available_ids()
        for role, spec in new_specs:
            if _in_list(spec, available) is False:
                raise _bad(
                    f"{role}: {llm_config.model_id(spec)!r} is not in Gemini's models.list "
                    "(retired, or a typo). See GET /admin/settings/llm/models."
                )

    # Everything validated; only now write, so nothing is half-applied.
    if "primary" in fields:
        if payload.primary is None:
            delete_setting(session, llm_config.primary_key(use))
        else:
            set_setting(session, llm_config.primary_key(use), primary)
    if "backups" in fields:
        if backups is None:
            delete_setting(session, llm_config.backups_key(use))
        else:
            set_setting(session, llm_config.backups_key(use), json.dumps(list(backups)))
    return available


@router.put("/llm/{use}", response_model=LLMUseRead)
def update_llm_settings(
    use: str,
    payload: LLMUseUpdate,
    session: Session = Depends(get_session),
) -> LLMUseRead:
    """
    Set or reset one use's primary and/or ordered backups. Takes effect on the
    next LLM call in this worker (other workers within 30 s) -- no redeploy.

    Rejected (400): malformed spec, unsupported provider, a model missing from
    Gemini's models.list, a duplicate backup, a backup equal to the primary,
    more than 2 backups for the agent, or a processor model without a
    rate-limit budget. If models.list cannot be fetched, a well-formed value is
    accepted -- the list is a typo guard, not a dependency. 404: unknown use.
    """
    if use not in llm_config.USES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown use {use!r}. Expected one of: {', '.join(llm_config.USES)}.")
    available = _apply_update(session, use, payload)
    return _use_read(use, available)


@router.get("/llm/models", response_model=AvailableModelsRead)
def list_llm_models(
    refresh: bool = Query(False, description="Bypass the 1-hour cache and refetch from Gemini."),
) -> AvailableModelsRead:
    """Models the Gemini API offers for generateContent, for the dropdowns.
    No rate limits, quota or pricing in here -- see `note`."""
    try:
        models, fetched_at = llm_config.model_list(refresh=refresh)
    except Exception as e:
        logger.warning("Gemini models.list failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Gemini models.list failed: {type(e).__name__}")

    configured: dict[str, list[str]] = {}
    for use in llm_config.USES:
        for role, spec in _roles(llm_config.resolve(use)):
            configured.setdefault(llm_config.model_id(spec), []).append(f"{use}.{role}")
    names = {m["name"] for m in models}
    return AvailableModelsRead(
        models=[AvailableModel(**m, configured_for=configured.get(m["name"], [])) for m in models],
        fetched_at=datetime.fromtimestamp(fetched_at, tz=timezone.utc),
        configured_missing=sorted(f"{m} ({', '.join(c)})" for m, c in configured.items() if m not in names),
        note=llm_config.MODEL_LIST_NOTE,
    )


# --------------------------------------------------------------------------
# /processor-model -- kept for the admin processing page
# --------------------------------------------------------------------------
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
    # The full chain in the order it is tried: primary, then each backup.
    fallback_chain: list[str]
    options: list[ModelOption]


class ProcessorModelUpdate(BaseModel):
    # None resets to the default (the env value, LLM_PROCESSOR_MODEL).
    model: Optional[str] = None


def _processor_read() -> ProcessorModelRead:
    r = llm_config.resolve(llm_config.PROCESSOR)
    return ProcessorModelRead(
        model=llm_config.model_id(r.primary.value),
        is_default=r.primary.source == "env",
        default_model=llm_config.model_id(r.primary.env_default) or DEFAULT_EXTRACTION_MODEL,
        fallback_chain=[llm_config.model_id(spec) for _, spec in _roles(r)],
        options=[ModelOption(model=m, **limits) for m, limits in MODEL_OPTIONS.items()],
    )


@router.get("/processor-model", response_model=ProcessorModelRead)
def read_processor_model() -> ProcessorModelRead:
    """The model the AI processor will use on its next run, and the choices."""
    return _processor_read()


@router.put("/processor-model", response_model=ProcessorModelRead)
def update_processor_model(
    payload: ProcessorModelUpdate,
    session: Session = Depends(get_session),
) -> ProcessorModelRead:
    """
    Switch the extraction model. Takes effect on the next record: extraction
    resolves its models per record (categorisation, once per run).

    Only allow-listed models are accepted: the rate limiter paces against the
    model's TPM/RPM budget, and a name with no budget attached cannot be paced.
    """
    if payload.model is not None:
        try:
            llm_config.validate_spec(llm_config.PROCESSOR, payload.model)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown model {payload.model!r}. Choose one of: {', '.join(MODEL_OPTIONS)}.",
            )
    # Same path as PUT /llm/processor, so a primary that collides with a stored
    # backup list is refused here too. No models.list check: the allow-list
    # above is stricter, as it always was on this endpoint.
    _apply_update(
        session, llm_config.PROCESSOR, LLMUseUpdate(primary=payload.model), check_model_list=False,
    )
    return _processor_read()
