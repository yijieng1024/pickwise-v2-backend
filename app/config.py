from typing import Optional

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # db
    database_url: str
    
    # JWT Configuration
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080
    email_verification_token_expire_hours: int = 1

    # Transactional email — Brevo HTTP API (api.brevo.com/v3/smtp/email).
    #
    # HTTP, not SMTP, on purpose: Render's free instances block outbound
    # traffic to ports 25/465/587, so the previous smtplib path failed there
    # with "[Errno 101] Network is unreachable" while registration still
    # returned 201. Port 443 is not blocked.
    #
    # The sender address must be a *verified sender* in Brevo, not merely an
    # address on the authenticated domain — Brevo rejects the send otherwise.
    brevo_api_key: str
    email_sender_address: str = "noreply@ngyijie.com"
    email_sender_name: str = "PickWise"

    # Public base URLs used to build links inside emails.
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"

    # Gemini API
    gemini_api_key: str

    # Timeouts and attempts for every Gemini call. The library defaults are
    # timeout=None and max_retries=6: a stalled call never fails, it waits, and
    # whatever the caller holds (a DB connection, a request slot) waits with
    # it. A fallback model can only fire if the primary is allowed to fail.
    #
    # llm_timeout_s reaches httpx as a per-READ limit, not a total, so
    # llm_total_deadline_s is the real bound: wall-clock for the whole call
    # across attempts, or until the first chunk for a stream (app/common/llm.py).
    # max_retries is the TOTAL attempt count in langchain-google-genai /
    # google-genai (HttpRetryOptions.attempts): 1 means one attempt on the same
    # model -- the backup model, not a second try, is the retry (Phase 2).
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 1
    llm_total_deadline_s: float = 60.0
    embedding_timeout_s: float = 15.0
    embedding_max_retries: int = 2

    # get_review_evidence drops chunks farther than this cosine distance from
    # the query. A guard against filler when nothing relevant exists, NOT a
    # precision filter: relevant and irrelevant distances overlap heavily
    # (median 0.353 vs 0.417), so ranking within the laptop's family does the
    # real work. Derived on 2026-10-22 from keyword-labelled data (14 realistic
    # and 3 off-topic queries against 346 chunks); re-derive it after Review
    # C's topic tags, or once there are more real agent queries. Tied to the
    # embedding model, like the 0.53 gate.
    review_evidence_max_distance: float = 0.44

    # Blocks POST /reviews/process/{id} and /reviews/process-bulk, which write
    # the Review C prompt's output straight to the public tables. On until the
    # C2 accuracy check is done; lift with REVIEW_PROCESSING_PAUSED=false.
    # The staging re-run (app/scripts/reprocess_reviews) is not affected.
    review_processing_paused: bool = True

    # Chat models per use, "provider:model": a primary and an ORDERED,
    # comma-separated list of backups (empty = no fallback). These are the
    # DEFAULTS: an admin override in app_settings (`<use>.model` /
    # `<use>.backup_models`) wins, and an invalid override falls back to these
    # (app/common/llm_config.py). Backups are tried in order when a model 429s,
    # times out or 5xxs; the agent allows at most 2.
    # Not covered, on purpose: the eval judge (eval/run_eval.py, fixed so runs
    # stay comparable) and embeddings (one vector space, one 0.53 threshold).
    llm_agent_model: str = "google_genai:gemma-4-31b-it"
    llm_agent_backup_models: str = "google_genai:gemma-4-26b-it"
    llm_review_processor_model: str = "google_genai:gemma-4-31b-it"
    llm_review_processor_backup_models: str = "google_genai:gemma-4-26b-it"
    # The processor's pre-Phase-2 overload chain, kept as its default.
    llm_processor_model: str = "google_genai:gemma-4-31b-it"
    llm_processor_backup_models: str = (
        "google_genai:gemma-4-26b-it,google_genai:gemini-3.5-flash-lite,google_genai:gemini-3.1-flash-lite"
    )

    # The env value is the last resort for an invalid app_settings row, so it
    # has nothing to fall back to: a bad one fails startup instead. Each primary
    # is declared before its backups, so `info.data` holds it below.
    @field_validator(
        "llm_agent_model", "llm_review_processor_model", "llm_processor_model",
    )
    @classmethod
    def _valid_model_spec(cls, value: str, info: ValidationInfo) -> str:
        from app.common.llm_config import validate_spec  # imports this module

        return validate_spec(info.field_name.removeprefix("llm_").removesuffix("_model"), value)

    @field_validator(
        "llm_agent_backup_models", "llm_review_processor_backup_models",
        "llm_processor_backup_models",
    )
    @classmethod
    def _valid_backup_list(cls, value: str, info: ValidationInfo) -> str:
        from app.common.llm_config import split_env_list, validate_backups

        use = info.field_name.removeprefix("llm_").removesuffix("_backup_models")
        primary = info.data.get(f"llm_{use}_model")
        if primary is None:  # the primary already failed; that error suffices
            return value
        return ",".join(validate_backups(use, split_env_list(value), primary))

    # Google Sign-In (OAuth web client ID) — optional; POST /auth/google
    # Google ID tokens sent by the frontend.
    google_oauth_client_id: Optional[str] = None

    # YouTube Data API v3 — optional until key is obtained; discovery.py raises on None
    youtube_api_key: Optional[str] = None

    # Webshare rotating-residential proxy (proxy.webshare.io) — optional;
    # transcript fetches go direct when unset. Needed on cloud hosts (Render)
    # because YouTube IP-blocks the unauthenticated transcript endpoint for
    # datacenter IPs; the YouTube Data API (discovery) is unaffected.
    webshare_proxy_username: Optional[str] = None
    webshare_proxy_password: Optional[str] = None

    # SerpApi (serpapi.com Google Shopping, gl=my) — optional; the
    # live-listings layer of search_malaysian_market_price reports
    # "unavailable" when unset and the tool answers from the catalog layer
    # + marketplace search links
    serp_api_key: Optional[str] = None

    # Comma-separated origins allowed to call the API from a browser
    # (CORS). Add the deployed frontend origin here in production.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,https://pickwise-eight.vercel.app,https://pickwise-v2.vercel.app,https://pickwise.ngyijie.com"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

class _Lazy:
    """
    Defers construction of an expensive-or-configured object until something
    actually uses it, while staying importable as a plain module-level name.

    WHY A PROXY AND NOT A get_x() FUNCTION. Callers write
    `from app.database import engine` and `Session(engine)`; a module-level
    __getattr__ would fire on that import statement, which is the importing
    module's import time -- exactly as eager as before. Only an object that is
    cheap to build and resolves on ATTRIBUTE ACCESS defers past the import.

    Verified against SQLAlchemy: `Session(proxy)` is accepted and builds
    nothing, and the real engine is created on the first query. So a test that
    stubs `Session` never constructs one at all.
    """

    __slots__ = ("_factory", "_real", "_label")

    def __init__(self, factory, label):
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_real", None)
        object.__setattr__(self, "_label", label)

    def _resolve(self):
        if object.__getattribute__(self, "_real") is None:
            object.__setattr__(self, "_real", object.__getattribute__(self, "_factory")())
        return object.__getattribute__(self, "_real")

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __setattr__(self, name, value):
        setattr(self._resolve(), name, value)

    # Equal to, and hashing like, the object it stands for. Not cosmetic:
    # SQLAlchemy files a session's connection under `conn.engine` (the real
    # Engine) and looks it up again with `bind in self._connections`, where
    # bind is whatever was passed to Session(...) -- this proxy. With identity
    # hashing that lookup always missed, so Session(engine) opened a NEW pooled
    # connection for every statement and held all of them until close: the
    # pool exhaustion, and a later query not seeing the same session's flushed
    # writes. __eq__ never resolves (nothing can equal an object not yet
    # built); __hash__ resolves, but SQLAlchemy only hashes the bind on the way
    # to bind.connect(), which resolves anyway.
    def __eq__(self, other):
        return other is self or other is object.__getattribute__(self, "_real")

    def __hash__(self):
        return hash(self._resolve())

    def __repr__(self):
        built = object.__getattribute__(self, "_real") is not None
        label = object.__getattribute__(self, "_label")
        return f"<lazy {label} ({'built' if built else 'not built yet'})>"


def _build_settings() -> "Settings":
    return Settings()  # type: ignore[call-arg]


# Lazy for the same reason the engine is (see app/database.py): `Settings()`
# validates five REQUIRED fields, so importing ANY module that reads settings
# demanded a full .env at import time. That is what made the unit tier
# impossible to run without secrets -- masked for months because a developer
# machine always has a .env on disk, the same way production only ever worked
# because Supabase happened to provision pgvector.
#
# Validation is not weakened, only moved from import to first use.
# app/main.py touches it during startup so a misconfigured deploy still fails
# before it serves a request, which is the property that actually mattered.
settings = _Lazy(_build_settings, "Settings")