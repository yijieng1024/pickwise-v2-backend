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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()