from typing import Optional

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