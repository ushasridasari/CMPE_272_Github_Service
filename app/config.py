"""Environment-driven configuration (12-factor).

Every secret and every deployment-specific value arrives through the
environment.  Nothing is hard-coded and nothing is committed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the Issues Gateway service.

    The five environment variable names below are fixed by the assignment
    specification and must not be renamed.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Required, names fixed by spec -------------------------------------
    github_token: str = Field(..., description="Fine-grained PAT or App installation token")
    github_owner: str = Field(..., description="Repository owner, e.g. 'andrewbond'")
    github_repo: str = Field(..., description="Repository name, e.g. 'cmpe272-issues-gw'")
    webhook_secret: str = Field(..., description="Shared secret used for webhook HMAC")
    port: int = Field(8080, description="Port this service listens on")

    # --- Optional tuning knobs ---------------------------------------------
    github_api_base: str = "https://api.github.com"
    github_api_version: str = "2022-11-28"
    request_timeout_seconds: float = 15.0
    events_db_path: str = "data/events.db"
    enable_etag_cache: bool = True
    log_level: str = "INFO"

    @property
    def repo_path(self) -> str:
        """The `owner/repo` fragment used to build GitHub URLs."""
        return f"{self.github_owner}/{self.github_repo}"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    Cached so the `.env` file is read once per process.  Tests call
    ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()  # type: ignore[call-arg]
