"""FastAPI application factory."""
from __future__ import annotations

from fastapi import FastAPI

from app.api.deps import rules_repo
from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import init_db


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    app = FastAPI(title="Iqama Screener", version="0.1.0",
                  description="Saudi Iqama screening and permit-file generation (MVP). Card-image based; not an official verification.")
    app.include_router(router)

    @app.get("/health")
    def health():
        snap = rules_repo().active
        return {"status": "ok", "rules_version": snap.version, "ocr_provider": settings.ocr_provider or snap.config.ocr.provider}

    return app


app = create_app()
