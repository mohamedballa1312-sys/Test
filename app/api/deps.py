"""Shared dependencies: settings, rules, services, API-key auth, actor identity."""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, Header, HTTPException, Request

from app.core.clock import Clock
from app.core.config import get_settings
from app.engines.rules import RulesRepository
from app.services.batch import BatchService
from app.services.permit_export import PermitExportService
from app.services.report import ReportService
from app.services.review import ReviewService


@lru_cache(maxsize=1)
def rules_repo() -> RulesRepository:
    return RulesRepository(get_settings().config_dir)


@lru_cache(maxsize=1)
def clock() -> Clock:
    return Clock(rules_repo().active.config.expiry.timezone)


@lru_cache(maxsize=1)
def batch_service() -> BatchService:
    return BatchService(rules_repo(), clock())


@lru_cache(maxsize=1)
def review_service() -> ReviewService:
    return ReviewService(rules_repo(), clock())


@lru_cache(maxsize=1)
def report_service() -> ReportService:
    return ReportService(rules_repo())


@lru_cache(maxsize=1)
def permit_service() -> PermitExportService:
    return PermitExportService(rules_repo(), batch_service(), clock())


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def actor(request: Request, x_actor: str | None = Header(default=None)) -> str:
    """MVP identity: caller-supplied X-Actor header (single-tenant, behind the API key). Phase 5: JWT/RBAC."""
    return (x_actor or "operator").strip()[:64]


def reset_deps() -> None:
    for f in (rules_repo, clock, batch_service, review_service, report_service, permit_service):
        f.cache_clear()
