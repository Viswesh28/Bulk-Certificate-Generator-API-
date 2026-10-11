"""Application settings.

Everything is configurable via environment variables (or a local ``.env``
file, see ``.env.example``). Tests construct ``Settings`` directly with
temporary paths, which is why nothing here is a module-level singleton.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Bulk Certificate Generator"
    api_v1_prefix: str = "/api/v1"

    # Any SQLAlchemy database URL. SQLite for zero-setup development,
    # PostgreSQL (or any other relational DB) in production.
    database_url: str = "sqlite:///./certificates.db"

    # Root directory for generated PDF files (local-disk storage backend).
    storage_dir: Path = Path("storage/certificates")

    # How many jobs may be rendered concurrently by the in-process worker.
    # Rendering is CPU-bound, so a small pool is deliberate (see README).
    worker_concurrency: int = Field(default=2, ge=1, le=16)

    # Guardrail: reject absurdly large single requests; clients split lists.
    max_recipients_per_job: int = Field(default=1000, ge=1)

    # Origins allowed to call the API from a browser. "*" suits the demo
    # preview; restrict it to your front-end domain in production.
    cors_allow_origins: list[str] = ["*"]


@lru_cache
def get_settings() -> Settings:
    """Cached default settings for the module-level app entrypoint."""
    return Settings()
