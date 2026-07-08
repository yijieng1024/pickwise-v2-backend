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

    # YouTube Data API v3 — optional until key is obtained; discovery.py raises on None
    youtube_api_key: Optional[str] = None

    # parse.bot iPrice Malaysia API — optional; search_malaysian_market_price
    # falls back to returning Shopee/Lazada search links when unset
    parsebot_api_key: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings() # type: ignore