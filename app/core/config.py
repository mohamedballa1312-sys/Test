"""Runtime settings (environment-driven). Business rules live in config/, not here."""
from __future__ import annotations

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
    api_key: str | None = None  # REQUIRED (P0-01): every request must carry it as X-API-Key
    allow_insecure: bool = False  # P0-01/02: only for local development - skips the API-key and HTTPS checks
    public_url: str | None = None  # P0-02: production must be served over https://
    ocr_provider: str | None = None  # overrides rules.yaml:ocr.provider
    workers: int = 2
    log_level: str = "INFO"
    api_base_url: str = "http://127.0.0.1:8000"  # used by the Streamlit client
    purge_interval_hours: int = 24  # P0-07: retention purge schedule (0 disables)
    max_files_per_upload: int = 200  # P0-08: upload count cap per request

    def validate_for_startup(self) -> list[str]:
        """P0-01/P0-02: refuse to start insecurely unless explicitly allowed for local development."""
        problems: list[str] = []
        if self.allow_insecure:
            return problems
        if not self.api_key or len(self.api_key) < 16:
            problems.append("IQAMA_API_KEY is missing or shorter than 16 characters (set IQAMA_ALLOW_INSECURE=1 only for local development)")
        if self.public_url and not self.public_url.lower().startswith("https://"):
            problems.append(f"IQAMA_PUBLIC_URL must be https:// in production (got {self.public_url})")
        return problems

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
