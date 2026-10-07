"""FastAPI application factory."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.deps import batch_service, rules_repo
from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import init_db


class InsecureStartupError(RuntimeError):
    pass


def _purge_loop(stop: threading.Event, interval_hours: int) -> None:
    """P0-07: retention purge on a schedule, every run audited by purge_expired itself."""
    from app.services.retention import purge_expired
    log = get_logger("retention")
    while not stop.wait(interval_hours * 3600):
        try:
            n = purge_expired(rules_repo(), actor="scheduler")
            log.info("retention purge", deleted_batches=n)
        except Exception as e:  # keep the loop alive
            log.error("retention purge failed", error=str(e))


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    problems = settings.validate_for_startup()
    if problems:
        raise InsecureStartupError("; ".join(problems))
    init_db()
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # P0-09: documents left in PROCESSING by a crash or restart are re-queued
        requeued = batch_service().sweep_stale(actor="startup")
        if requeued:
            get_logger("startup").info("re-queued stale documents", count=requeued)
        if settings.purge_interval_hours > 0:
            threading.Thread(target=_purge_loop, args=(stop, settings.purge_interval_hours), daemon=True, name="retention-purge").start()
        yield
        stop.set()

    app = FastAPI(title="Iqama Screener", version="0.1.0", lifespan=lifespan,
                  description="Saudi Iqama screening and permit-file generation (MVP). Card-image based; not an official verification.")
    app.include_router(router)

    @app.get("/health")
    def health():
        snap = rules_repo().active
        return {"status": "ok", "rules_version": snap.version, "ocr_provider": settings.ocr_provider or snap.config.ocr.provider}

    return app


app = create_app()
