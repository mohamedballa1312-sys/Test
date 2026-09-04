"""Check ② — Iqama validity against the real current date (never a constant)."""
from __future__ import annotations

from app.core.clock import Clock
from app.engines.models import CheckResult, ExtractionResult
from app.engines.rules import RulesSnapshot


def check_expiry(x: ExtractionResult, rules: RulesSnapshot, clock: Clock | None = None) -> CheckResult:
    cfg = rules.config.expiry
    clock = clock or Clock(cfg.timezone)
    today = clock.today()
    expiry = x.date_value("expiry_date")
    conf = x.confidence("expiry_date")
    details = {"expiry_date": expiry.isoformat() if expiry else None, "check_date": today.isoformat(),
               "days_remaining": None, "raw": x.raw("expiry_date"), "warn_days": cfg.warn_days}
    base = dict(check="EXPIRY", rules_version=rules.version)

    if expiry is None:
        return CheckResult(outcome="REVIEW", label="DATE_NOT_READABLE", confidence=conf, details=details,
                           reason="Expiry date unreadable", **base)
    days = (expiry - today).days
    details["days_remaining"] = days
    ev = [{"layer": "date_compare", "expiry": expiry.isoformat(), "today": today.isoformat(), "days": days}]
    if days < 0:
        return CheckResult(outcome="FAIL", label="EXPIRED", confidence=conf, details=details, evidence=ev,
                           reason=f"Iqama Expired ({expiry.isoformat()}, {days} days)", **base)
    if days <= cfg.warn_days:
        return CheckResult(outcome="REVIEW", label="EXPIRING_SOON", confidence=conf, details=details, evidence=ev,
                           reason=f"Expiring soon: {days} days remaining ({expiry.isoformat()})", **base)
    return CheckResult(outcome="PASS", label="VALID", confidence=conf, details=details, evidence=ev, **base)
