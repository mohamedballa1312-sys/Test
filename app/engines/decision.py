"""Decision engine — gates G0..G3 (Architecture §6). Deterministic: same input + same rules version = same output."""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.clock import Clock
from app.engines.employer import check_employer
from app.engines.expiry import check_expiry
from app.engines.models import CheckResult, Decision, ExtractionResult
from app.engines.nationality import check_nationality
from app.engines.occupation import check_occupation
from app.engines.rules import RulesSnapshot

_CHECKS = {
    "NATIONALITY": lambda x, r, c: check_nationality(x, r),
    "EXPIRY": check_expiry,
    "EMPLOYER": lambda x, r, c: check_employer(x, r),
    "OCCUPATION": lambda x, r, c: check_occupation(x, r),
}


def run_checks(x: ExtractionResult, rules: RulesSnapshot, clock: Clock | None = None) -> list[CheckResult]:
    return [_CHECKS[name](x, rules, clock) for name in rules.config.checks.order]


def _apply_cross_check_rules(checks: list[CheckResult], rules: RulesSnapshot) -> None:
    """Rules that depend on two checks at once (Rules v1.1)."""
    by = {c.check: c for c in checks}
    emp, occ = by.get("EMPLOYER"), by.get("OCCUPATION")
    if (rules.config.employer.individual_with_eligible_occupation == "REVIEW" and emp and occ
            and emp.outcome == "FAIL" and emp.label == "INDIVIDUAL"
            and occ.outcome == "PASS" and occ.label == "ELIGIBLE"):
        emp.outcome = "REVIEW"
        emp.reason = f"Individual employer with eligible occupation ({occ.details.get('matched_ar') or occ.details.get('raw')}) - human decision"
        emp.evidence.append({"layer": "xrule", "signal": "individual_with_eligible_occupation", "action": "REVIEW"})


def decide(x: ExtractionResult, rules: RulesSnapshot, clock: Clock | None = None, *,
           decided_by: str = "system", version: int = 1) -> Decision:
    cfg = rules.config
    clock = clock or Clock(cfg.expiry.timezone)
    checks = run_checks(x, rules, clock)
    _apply_cross_check_rules(checks, rules)
    now = clock.now()
    base = dict(checks=checks, rules_version=rules.version, decided_at=now, decided_by=decided_by,
                version=version, quality_score=x.quality_score)

    # ---- G0: extraction quality / completeness ----
    triggers: list[str] = []
    if x.quality_score < cfg.image.min_quality_score:
        # nothing read from a poor image is trustworthy, including a would-be hard fail
        return Decision(status="MANUAL_REVIEW", recommendation="NEEDS_ATTENTION",
                        review_triggers=[f"POOR_IMAGE (quality {x.quality_score:.2f} < {cfg.image.min_quality_score})"], **base)
    for f in cfg.ocr.critical_fields:
        fv = x.fields.get(f)
        if fv is None or fv.normalized in (None, ""):
            triggers.append(f"MISSING_FIELD:{f}")
        elif fv.source == "ocr" and fv.confidence < cfg.ocr.min_field_confidence:
            triggers.append(f"LOW_CONFIDENCE:{f} ({fv.confidence:.2f})")

    # ---- G1: hard fails (all reasons collected, in check order) ----
    hard = [c for c in checks if c.outcome == "FAIL" and c.confidence >= cfg.decision.hard_fail_min_confidence]
    soft_fail = [c for c in checks if c.outcome == "FAIL" and c.confidence < cfg.decision.hard_fail_min_confidence]
    if hard:
        reasons = [c.reason or c.label for c in hard]
        # failures the engine could not confirm are still surfaced (report + reviewer), never silently dropped
        soft_notes = [f"{c.check}: {c.reason} (unconfirmed, confidence {c.confidence:.2f})" for c in soft_fail]
        if cfg.decision.require_human_confirmation_on_reject:
            return Decision(status="MANUAL_REVIEW", reasons=reasons, review_triggers=triggers + soft_notes + ["HUMAN_CONFIRMATION_REQUIRED"],
                            recommendation="RECOMMEND_REJECT", **base)
        return Decision(status="REJECTED", reasons=reasons, review_triggers=triggers + soft_notes, recommendation=None, **base)

    # ---- G2: ambiguity ----
    for c in checks:
        if c.outcome in ("REVIEW", "UNKNOWN"):
            triggers.append(f"{c.check}: {c.reason or c.label}")
    for c in soft_fail:
        triggers.append(f"{c.check}: {c.reason} (low confidence {c.confidence:.2f})")
    if triggers:
        return Decision(status="MANUAL_REVIEW", review_triggers=triggers, recommendation="NEEDS_ATTENTION", **base)

    # ---- G3: all PASS ----
    if cfg.decision.auto_approve:
        return Decision(status="APPROVED", **base)
    return Decision(status="MANUAL_REVIEW", review_triggers=["ALL_CHECKS_PASSED_AWAITING_HUMAN_APPROVAL"],
                    recommendation="RECOMMEND_APPROVE", **base)
