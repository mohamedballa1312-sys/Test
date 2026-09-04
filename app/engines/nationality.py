"""Check ① — nationality. Runs first (Rules v1 D1)."""
from __future__ import annotations

from app.engines.models import CheckResult, ExtractionResult
from app.engines.rules import RulesSnapshot


def check_nationality(x: ExtractionResult, rules: RulesSnapshot) -> CheckResult:
    cfg = rules.config.nationality
    code = x.value("nationality")
    raw = x.raw("nationality")
    conf = x.confidence("nationality")
    base = dict(check="NATIONALITY", rules_version=rules.version, details={"raw": raw, "code": code, "mode": cfg.mode})

    if not code:
        return CheckResult(outcome="REVIEW", label="UNREADABLE", confidence=conf,
                           reason="Nationality unreadable or not recognised", **base)
    row = rules.nationality_by_code(code)
    name_en = row.name_en if row else code
    evidence = [{"layer": "lookup", "matched": code, "mode": cfg.mode}]

    if cfg.mode == "ALL_APPROVED":
        return CheckResult(outcome="PASS", label="APPROVED", confidence=conf, evidence=evidence, **base)
    approved = bool(row and row.approved)
    if cfg.mode == "BLOCKLIST":
        if row and not row.approved:
            return CheckResult(outcome="FAIL", label="NOT_APPROVED", confidence=conf, evidence=evidence,
                               reason=f"Nationality not approved: {name_en}", **base)
        return CheckResult(outcome="PASS", label="APPROVED", confidence=conf, evidence=evidence, **base)
    # ALLOWLIST
    if approved:
        return CheckResult(outcome="PASS", label="APPROVED", confidence=conf, evidence=evidence, **base)
    return CheckResult(outcome="FAIL", label="NOT_APPROVED", confidence=conf, evidence=evidence,
                       reason=f"Nationality not in approved list: {name_en}", **base)
