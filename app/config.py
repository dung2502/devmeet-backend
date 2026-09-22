from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="development", alias="APP_ENV")
    secret_key: str = Field(default="dev-secret-key", alias="SECRET_KEY")
    database_url: str = Field(
        default="postgresql+psycopg2://devmeet:devmeet@localhost:5432/devmeet_db",
        alias="DATABASE_URL",
    )
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(default="", alias="GOOGLE_REDIRECT_URI")
    n8n_webhook_url: str = Field(
        default="http://localhost:5678/webhook/devmeet-meeting-ai",
        alias="N8N_WEBHOOK_URL",
    )
    devmeet_webhook_secret: str = Field(
        default="dev-webhook-secret-change-in-production-min-32-chars",
        alias="DEVMEET_WEBHOOK_SECRET",
    )
    n8n_timeout_seconds: float = Field(
        default=120.0,
        alias="N8N_TIMEOUT_SECONDS",
    )
    devmeet_spreadsheet_id: str = Field(
        default="",
        alias="DEVMEET_SPREADSHEET_ID",
    )
    allow_dev_auth_bypass: bool = Field(
        default=False,
        alias="ALLOW_DEV_AUTH_BYPASS",
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:3000", "http://127.0.0.1:5173"],
        alias="DEVMEET_CORS_ORIGINS",
    )
    jwt_secret_key: str = Field(
        default="devmeet-secret-key-change-in-production-min-32-chars",
        alias="JWT_SECRET_KEY",
    )
    jwt_algorithm: str = Field(
        default="HS256",
        alias="JWT_ALGORITHM",
    )
    access_token_expire_minutes: int = Field(
        default=30,
        alias="ACCESS_TOKEN_EXPIRE_MINUTES",
    )
    refresh_token_expire_days: int = Field(
        default=30,
        alias="REFRESH_TOKEN_EXPIRE_DAYS",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
