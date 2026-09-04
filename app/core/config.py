"""Runtime settings (environment-driven). Business rules live in config/, not here."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IQAMA_", env_file=".env", extra="ignore")

    config_dir: Path = Field(default=PROJECT_ROOT / "config")
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    db_url: str | None = None  # default: sqlite in data_dir
    enc_key: str | None = None  # base64 32-byte key; generated into data_dir/.enc_key if absent
    api_key: str | None = None  # if set, required as X-API-Key on every request
    ocr_provider: str | None = None  # overrides rules.yaml:ocr.provider
    workers: int = 2
    log_level: str = "INFO"
    api_base_url: str = "http://127.0.0.1:8000"  # used by the Streamlit client

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def effective_db_url(self) -> str:
        return self.db_url or f"sqlite:///{self.data_dir / 'iqama.db'}"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
        _settings.images_dir.mkdir(parents=True, exist_ok=True)
    return _settings


def reset_settings() -> None:
    """Testing helper."""
    global _settings
    _settings = None
