"""Check ④ — occupation, against the editable occupations.csv (ALLOWLIST by default: unlisted -> REVIEW)."""
from __future__ import annotations

from rapidfuzz import fuzz, process

from app.core.text import normalize_arabic
from app.engines.models import CheckResult, ExtractionResult
from app.engines.rules import OccupationRow, RulesSnapshot


def match_occupation(text: str | None, rules: RulesSnapshot) -> tuple[OccupationRow | None, float, str]:
    """Returns (row, confidence, method)."""
    if not text:
        return None, 0.0, "none"
    norm = normalize_arabic(text)
    row = rules.occupation_exact(norm)
    if row:
        return row, 1.0, "exact"
    row = rules.occupation_alias(norm)
    if row:
        return row, 0.95, "alias"
    cands = rules.occupation_candidates()
    if not cands:
        return None, 0.0, "none"
    keys = [k for k, _ in cands]
    best = process.extractOne(norm, keys, scorer=fuzz.token_set_ratio)
    if best:
        key, score, idx = best
        score = score / 100.0
        if score >= rules.config.occupation.fuzzy_threshold:
            return cands[idx][1], round(score, 3), "fuzzy"
    return None, 0.0, "none"


def check_occupation(x: ExtractionResult, rules: RulesSnapshot) -> CheckResult:
    cfg = rules.config.occupation
    raw = x.value("occupation") or x.raw("occupation")
    field_conf = x.confidence("occupation")
    row, mconf, method = match_occupation(raw, rules)
    conf = round(min(mconf, field_conf) if field_conf else mconf, 3)
    if method in ("exact", "alias") and row is not None:
        conf = max(conf, 0.80)  # exact vocabulary hit is strong evidence, but a floor never reaches the auto-reject threshold alone
    base = dict(check="OCCUPATION", rules_version=rules.version,
                details={"raw": raw, "matched_ar": row.occupation_ar if row else None, "matched_en": row.occupation_en if row else None,
                         "category": row.category if row else None, "method": method, "match_confidence": mconf})
    if raw is None:
        return CheckResult(outcome="REVIEW", label="UNREADABLE", confidence=0.0, reason="Occupation unreadable", **base)
    if row is None:
        if cfg.model == "BLOCKLIST":
            return CheckResult(outcome="PASS", label="NOT_EXCLUDED", confidence=conf,
                               evidence=[{"layer": "blocklist", "signal": "not_in_excluded_list"}], **base)
        return CheckResult(outcome="REVIEW", label="UNKNOWN", confidence=conf,
                           reason=f"Occupation not in reference list: {raw}", **base)
    ev = [{"layer": method, "matched": row.occupation_ar, "score": mconf}]
    if not row.eligible:
        label = f"{row.occupation_en} ({row.occupation_ar})" if row.occupation_en else row.occupation_ar
        if method == "fuzzy" and mconf < cfg.review_below_confidence:
            return CheckResult(outcome="REVIEW", label="EXCLUDED_FUZZY", confidence=conf, evidence=ev,
                               reason=f"Possible excluded occupation: {raw} ≈ {row.occupation_ar} ({mconf:.2f})", **base)
        return CheckResult(outcome="FAIL", label="EXCLUDED", confidence=conf, evidence=ev,
                           reason=f"Excluded Occupation: {label}", **base)
    if mconf < cfg.review_below_confidence:
        return CheckResult(outcome="REVIEW", label="ELIGIBLE_FUZZY", confidence=conf, evidence=ev,
                           reason=f"Fuzzy occupation match: {raw} ≈ {row.occupation_ar} ({mconf:.2f})", **base)
    return CheckResult(outcome="PASS", label="ELIGIBLE", confidence=conf, evidence=ev, **base)
