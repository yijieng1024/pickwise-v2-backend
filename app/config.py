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

    # SMTP Configuration
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_username: str
    smtp_password: str

    # Gemini API
    gemini_api_key: str

    # Google Sign-In (OAuth web client ID) — optional; POST /auth/google
    # returns 400 when unset. Used only as the audience check when verifying
    # Google ID tokens sent by the frontend.
    google_oauth_client_id: Optional[str] = None

    # YouTube Data API v3 — optional until key is obtained; discovery.py raises on None
    youtube_api_key: Optional[str] = None

    # Serper.dev (Google Shopping, gl=my) — optional; the live-listings layer
    # of search_malaysian_market_price reports "unavailable" when unset and
    # the tool answers from the catalog layer + marketplace search links
    serper_api_key: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings() # type: ignore