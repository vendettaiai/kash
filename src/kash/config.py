"""Application configuration settings using Pydantic Settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    """Central configuration for Kash."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="Kash", description="Application name")
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    # Kash's own log lines (bank sync steps, scheduler) on stderr: DEBUG, INFO, WARNING, ...
    log_level: str = "INFO"
    secret_key: str = Field(
        default="kash-dev-insecure-secret-key-change-in-production-min32chars",
        description="Secret key for signing sessions and tokens",
    )

    # Database
    # Either DATABASE_URL, or the POSTGRES_* parts (e.g. separate fields in Unraid's UI).
    postgres_user: str = "kash_user"
    postgres_password: str = "kash_secure_password"
    postgres_db: str = "kash_db"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    database_url: str = ""

    # Security & Encryption
    data_encryption_key: str = Field(
        default="",
        description="Fernet/AES-GCM 32-byte urlsafe base64 key for encrypting provider credentials",
    )
    # Optional: required on the first-run "create your account" form, so nobody else
    # can claim a freshly started instance that is reachable from the internet.
    setup_token: str = ""
    session_cookie_name: str = "kash_session"
    session_expire_seconds: int = 604800  # 7 days
    secure_cookies: bool = False

    # Bank Synchronization
    simplefin_max_requests_per_day: int = 24
    sync_interval_hours: int = 12  # unused; see sync_schedule_hours
    # Automatic sync at these hours of the day (comma-separated, 0-23), e.g.
    # "0,4,8,12,16,20". Empty (the default) = manual sync only.
    sync_schedule_hours: str = ""
    sync_timezone: str = "America/New_York"

    # AI Feature Flags (Phase 9)
    ai_enabled: bool = False
    ai_api_key: str = ""

    @model_validator(mode="after")
    def _database_url_from_parts(self) -> "Settings":
        """Build DATABASE_URL from POSTGRES_* when it isn't set (special characters in
        the password are escaped)."""
        if not self.database_url:
            self.database_url = URL.create(
                "postgresql+asyncpg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                database=self.postgres_db,
            ).render_as_string(hide_password=False)
        return self


INSECURE_SECRET_KEYS = {
    "kash-dev-insecure-secret-key-change-in-production-min32chars",
    "change-this-to-a-secure-random-string-at-least-32-chars-long",
    "replace-with-a-production-secret-key-at-least-32-chars",
}


def check_production_secrets(settings: Settings) -> None:
    """Refuse to start in production with a missing, short or example SECRET_KEY."""
    if settings.environment != "production":
        return
    if len(settings.secret_key) < 32 or settings.secret_key in INSECURE_SECRET_KEYS:
        raise RuntimeError(
            "SECRET_KEY must be set to a random value of at least 32 characters in production "
            '(for example: python -c "import secrets; print(secrets.token_urlsafe(48))").'
        )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
