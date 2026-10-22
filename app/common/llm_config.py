"""
Which chat models each feature uses: a primary plus an ORDERED list of backups,
per use, switchable by an admin without a redeploy.

STORAGE. The existing `app_settings` key/value table, two keys per use:
`<use>.model` (primary) and `<use>.backup_models`, a JSON array of specs in
the order they are tried. JSON because it keeps order, cannot be split wrong,
and `[]` ("no fallback") is a value distinct from "no row" ("env default").
The processor's pre-existing `processor.model` key already fits, so it is used
as-is. A bare model name ("gemma-4-31b-it", which is what that row holds
today) means the google_genai provider, so old rows stay valid.

RESOLUTION: a valid app_settings row wins; no row -> the env default
(`Settings.llm_<use>_model` / `_backup_models`, the latter comma-separated); an
INVALID row -> logged, the env default is used, and the error is reported by
the admin GET so a human can see why their choice is not in effect. A backup
list is valid or invalid AS A WHOLE: a half-applied list is an order nobody
chose. app_settings validates nothing, so the check lives here, on read as
well as on write: a row outlives any deploy that changes what is valid.

CACHE. Resolved values live in-process for `_CACHE_TTL_S`. `set_setting` /
`delete_setting` bump `app_settings.generation()`, which empties this cache in
the worker that took the write; other workers catch up within the TTL.
"""
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from sqlmodel import Session, select

from app.common import app_settings
from app.common.app_settings import AppSetting
from app.config import settings
from app.database import engine
from app.logger import get_logger

logger = get_logger(__name__)

AGENT = "agent"
REVIEW_PROCESSOR = "review_processor"
PROCESSOR = "processor"
USES = (AGENT, REVIEW_PROCESSOR, PROCESSOR)

SUPPORTED_PROVIDERS = ("google_genai",)
_DEFAULT_PROVIDER = "google_genai"
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

# The catalog processor's selectable models WITH their free-tier budgets. Its
# rate limiter paces against TPM/RPM per model, so a model with no numbers
# attached cannot be paced -- the processor alone is restricted to this list.
# Read from this account's dashboard (https://aistudio.google.com/rate-limit,
# 2026-09-22). RPD decides the list: every Gemini *flash* model is capped at 20
# requests/day on this account, so a 100-record batch would die after 20.
# `rpd` is advisory; nothing enforces a daily count.
# ponytail: no RPD enforcement -- add a counter only if a 500/day model
# becomes the one doing the daily catalog runs.
MODEL_OPTIONS: dict[str, dict[str, int]] = {
    "gemma-4-31b-it": {"tpm": 16_000, "rpm": 30, "rpd": 14_400},
    "gemma-4-26b-it": {"tpm": 16_000, "rpm": 30, "rpd": 14_400},
    "gemini-3.5-flash-lite": {"tpm": 250_000, "rpm": 15, "rpd": 500},
    "gemini-3.1-flash-lite": {"tpm": 250_000, "rpm": 15, "rpd": 500},
}
_ALLOW_LISTS = {PROCESSOR: MODEL_OPTIONS}

# The user-facing use: every model in the chain may take the full 60 s
# deadline, so a user waits at most (1 + 2) x 60 s if every model hangs. The
# agent is a synchronous request; the background uses (review_processor,
# processor) are uncapped.
MAX_BACKUPS = {AGENT: 2}


def primary_key(use: str) -> str:
    return f"{use}.model"


def backups_key(use: str) -> str:
    return f"{use}.backup_models"


def parse_spec(value: str) -> tuple[str, str]:
    """'provider:model' -> (provider, model). A bare name is google_genai."""
    provider, sep, model = value.strip().partition(":")
    if not sep:
        provider, model = _DEFAULT_PROVIDER, provider
    return provider.strip(), model.strip().removeprefix("models/")


def model_id(spec: str) -> str:
    return parse_spec(spec)[1]


def validate_spec(use: str, value: str) -> str:
    """The normalized 'provider:model', or ValueError saying what is wrong."""
    if use not in USES:
        raise ValueError(f"Unknown use {use!r}. Expected one of: {', '.join(USES)}.")
    if not value or not value.strip():
        raise ValueError("Model must not be empty.")
    provider, model = parse_spec(value)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider {provider!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    if not _MODEL_ID_RE.match(model):
        raise ValueError(f"Malformed model id {model!r}.")
    allowed = _ALLOW_LISTS.get(use)
    if allowed is not None and model not in allowed:
        raise ValueError(
            f"{model!r} is not selectable for {use}: its rate limiter has no budget for it. "
            f"Choose one of: {', '.join(allowed)}."
        )
    return f"{provider}:{model}"


def validate_backups(use: str, values: list[str], primary: str) -> tuple[str, ...]:
    """The normalized ordered backups, or ValueError naming the broken rule."""
    out: list[str] = []
    for i, v in enumerate(values):
        try:
            spec = validate_spec(use, v)
        except ValueError as e:
            raise ValueError(f"backups[{i}]: {e}") from None
        if spec == primary:
            raise ValueError(f"backups[{i}]: {model_id(spec)!r} is the primary; a backup must differ from it.")
        if spec in out:
            raise ValueError(f"backups[{i}]: {model_id(spec)!r} is listed twice.")
        out.append(spec)
    cap = MAX_BACKUPS.get(use)
    if cap is not None and len(out) > cap:
        raise ValueError(f"{use} allows at most {cap} backups (got {len(out)}).")
    return tuple(out)


def split_env_list(value: str) -> list[str]:
    """Env form of a backup list: comma-separated; empty means no backups."""
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_stored_backups(raw: str) -> list[str]:
    try:
        values = json.loads(raw)
    except ValueError:
        raise ValueError("Malformed backup list: not JSON.") from None
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ValueError("Malformed backup list: expected a JSON array of strings.")
    return values


def env_default(use: str) -> str:
    return getattr(settings, f"llm_{use}_model")


def env_default_backups(use: str) -> tuple[str, ...]:
    return tuple(split_env_list(getattr(settings, f"llm_{use}_backup_models")))


@dataclass(frozen=True)
class Slot:
    value: str                     # normalized spec actually in effect
    source: str                    # "app_settings" | "env"
    env_default: str
    stored: Optional[str] = None   # raw app_settings value, if a row exists
    error: Optional[str] = None    # why `stored` was rejected


@dataclass(frozen=True)
class Backups:
    values: tuple[str, ...]        # in effect, in the order they are tried
    source: str                    # "app_settings" | "env"
    env_default: tuple[str, ...]
    stored: Optional[str] = None   # raw JSON row, if one exists
    error: Optional[str] = None    # why `stored` was rejected (the whole list)


@dataclass(frozen=True)
class UseModels:
    use: str
    primary: Slot
    backups: Backups


_CACHE_TTL_S = 30.0
_cache: dict[str, tuple[float, int, UseModels]] = {}
_lock = threading.Lock()


def _primary(use: str, stored: Optional[str]) -> Slot:
    default = env_default(use)
    if stored is None:
        return Slot(value=default, source="env", env_default=default)
    try:
        return Slot(value=validate_spec(use, stored), source="app_settings",
                    env_default=default, stored=stored)
    except ValueError as e:
        logger.warning(
            "Ignoring invalid app_settings %s=%r (%s); using env default %s.",
            primary_key(use), stored, e, default,
        )
        return Slot(value=default, source="env", env_default=default, stored=stored, error=str(e))


def _backups(use: str, stored: Optional[str], primary: str) -> Backups:
    default = env_default_backups(use)
    # The env list is checked at startup against the env primary only; under an
    # admin-chosen primary it may contain that model, which is then skipped.
    env_values = tuple(b for b in default if b != primary)
    if stored is None:
        return Backups(values=env_values, source="env", env_default=default)
    try:
        values = validate_backups(use, parse_stored_backups(stored), primary)
        return Backups(values=values, source="app_settings", env_default=default, stored=stored)
    except ValueError as e:
        logger.warning(
            "Ignoring invalid app_settings %s=%r (%s); using env default %s.",
            backups_key(use), stored, e, list(default),
        )
        return Backups(values=env_values, source="env", env_default=default, stored=stored, error=str(e))


def _read_rows(use: str) -> dict[str, str]:
    keys = (primary_key(use), backups_key(use))
    with Session(engine) as session:
        rows = session.exec(select(AppSetting).where(AppSetting.key.in_(keys))).all()  # type: ignore[attr-defined]
        return {r.key: r.value for r in rows}


def resolve(use: str) -> UseModels:
    """Primary and backups for `use`. Never raises for a DB problem: a failed
    read logs and serves the env defaults, because a settings outage must not
    take the chat down with it."""
    if use not in USES:
        raise ValueError(f"Unknown use {use!r}")
    gen = app_settings.generation()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(use)
        if hit and hit[1] == gen and now - hit[0] < _CACHE_TTL_S:
            return hit[2]
    try:
        rows = _read_rows(use)
    except Exception as e:
        # One line, no traceback: the env-default result below is cached for
        # the TTL like a successful read, so this fires once per window, not per
        # call -- and a settings outage is expected noise (CI with an
        # unreachable DB), not a bug whose stack anyone needs.
        logger.warning(
            "Reading model settings for %s failed (%s: %s); using env defaults for %.0fs.",
            use, type(e).__name__, str(e).strip().split("\n")[0][:200], _CACHE_TTL_S,
        )
        rows = {}
    primary = _primary(use, rows.get(primary_key(use)))
    resolved = UseModels(
        use=use, primary=primary, backups=_backups(use, rows.get(backups_key(use)), primary.value),
    )
    with _lock:
        _cache[use] = (now, gen, resolved)
    return resolved


def resolve_models(use: str) -> tuple[str, tuple[str, ...]]:
    """(primary, backups) specs -- the chain `build_llm` tries, in order."""
    r = resolve(use)
    return r.primary.value, r.backups.values


# --------------------------------------------------------------------------
# Gemini models.list -- fills the admin dropdown.
# --------------------------------------------------------------------------
_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_MODEL_LIST_TTL_S = 3600.0
_model_list: Optional[tuple[float, list[dict[str, Any]]]] = None

MODEL_LIST_NOTE = (
    "From the Gemini API models.list. It carries no rate limits, quota or "
    "pricing, and says nothing about tool-calling quality -- check AI Studio's "
    "rate-limit page and run the eval harness before trusting a model."
)


def parse_model_list(pages: list[dict]) -> list[dict[str, Any]]:
    """Models that support generateContent, from raw models.list pages. Only
    `name` is required: Gemma entries have been seen without `description`,
    and the other fields default rather than fail."""
    out = []
    for page in pages:
        for m in page.get("models") or []:
            name = m.get("name")
            if not name or "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            mid = name.removeprefix("models/")
            out.append({
                "name": mid,
                "spec": f"{_DEFAULT_PROVIDER}:{mid}",
                "display_name": m.get("displayName") or mid,
                "input_token_limit": m.get("inputTokenLimit"),
                "output_token_limit": m.get("outputTokenLimit"),
                "thinking": m.get("thinking"),
            })
    return out


def _fetch_pages() -> list[dict]:
    pages: list[dict] = []
    params: dict[str, Any] = {"pageSize": 1000}
    # Key in a header, not the query string, so it never lands in an access log.
    headers = {"x-goog-api-key": settings.gemini_api_key}
    with httpx.Client(timeout=15.0) as client:
        while True:
            resp = client.get(_MODELS_URL, params=params, headers=headers)
            resp.raise_for_status()
            page = resp.json()
            pages.append(page)
            token = page.get("nextPageToken")
            if not token:
                return pages
            params["pageToken"] = token


def model_list(refresh: bool = False) -> tuple[list[dict[str, Any]], float]:
    """(models, fetched_at epoch seconds). Cached an hour; `refresh` refetches.
    Raises on a fetch failure -- callers decide whether that is fatal."""
    global _model_list
    if not refresh and _model_list and time.time() - _model_list[0] < _MODEL_LIST_TTL_S:
        return _model_list[1], _model_list[0]
    models = parse_model_list(_fetch_pages())
    _model_list = (time.time(), models)
    return models, _model_list[0]
