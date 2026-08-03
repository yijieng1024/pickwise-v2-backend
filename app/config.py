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

    # SMTP Configuration.
    #
    # Note: this works locally but NOT on Render's free instances, which
    # block outbound traffic to SMTP ports (25/465/587) — sends there fail
    # with "[Errno 101] Network is unreachable".
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_username: str
    smtp_password: str

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
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,https://pickwise-eight.vercel.app"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings() # type: ignore