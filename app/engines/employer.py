"""Check ③ — employer classification. L0 (employer-ID prefix) is deterministic; name analysis is secondary."""
from __future__ import annotations

import re

from app.core.text import normalize_arabic
from app.engines.models import CheckResult, ExtractionResult
from app.engines.rules import RulesSnapshot

_PREFIX_CONF = 0.99


def classify_employer(employer_id: str | None, employer_name: str | None, rules: RulesSnapshot) -> tuple[str, float, list[dict]]:
    """Returns (employer_type, confidence, evidence). Pure function — no I/O."""
    cfg = rules.config.employer
    kw = rules.employer_keywords
    evidence: list[dict] = []
    name_norm = normalize_arabic(employer_name) or ""

    # ---- name-based signals (computed always; used as primary only if L0/L1 absent) ----
    name_type: str | None = None
    name_conf = 0.0
    if name_norm:
        if any(normalize_arabic(k) in name_norm for k in kw.government_keywords):
            name_type, name_conf = "GOVERNMENT", 0.95
            evidence.append({"layer": "L2", "signal": "government_keyword"})
        elif any(re.search(rf"(^|\s){re.escape(normalize_arabic(k))}(\s|$|\.)", name_norm) for k in kw.legal_form_keywords):
            name_type, name_conf = "COMPANY", 0.90
            evidence.append({"layer": "L3", "signal": "legal_form_keyword"})
        elif any(normalize_arabic(k) in name_norm for k in kw.activity_keywords):
            name_type, name_conf = "COMPANY", 0.75
            evidence.append({"layer": "L4", "signal": "activity_keyword"})
        elif any(re.search(p, name_norm) for p in kw.person_name_patterns):
            name_type, name_conf = "INDIVIDUAL", 0.60
            evidence.append({"layer": "L5", "signal": "person_name_pattern"})

    # ---- L1 reference DB ----
    ref = rules.employer_ref_by_id(employer_id) if employer_id else None
    if ref is None and name_norm:
        ref = rules.employer_ref_by_name(name_norm)
    if ref is not None:
        evidence.insert(0, {"layer": "L1", "signal": "reference_db", "source": ref.source})
        return ref.employer_type, 1.0, evidence

    # ---- L0 ID prefix ----
    if employer_id and len(employer_id) == 10 and employer_id[0] in cfg.id_prefix_map:
        etype = cfg.id_prefix_map[employer_id[0]]
        conf = _PREFIX_CONF
        evidence.insert(0, {"layer": "L0", "signal": "id_prefix", "prefix": employer_id[0], "type": etype})
        # secondary cross-check with the name: a contradiction lowers confidence below auto-reject threshold
        if name_type and name_type != etype and not (etype == "COMPANY" and name_type == "GOVERNMENT"):
            conf = round(conf - 0.30, 2)
            evidence.append({"layer": "xcheck", "signal": "name_contradicts_prefix", "name_type": name_type})
        elif name_type == etype:
            evidence.append({"layer": "xcheck", "signal": "name_confirms_prefix"})
        return etype, conf, evidence

    if employer_id:
        evidence.append({"layer": "L0", "signal": "unknown_prefix", "prefix": employer_id[:1]})

    # ---- name only ----
    if name_type:
        return name_type, name_conf, evidence
    return "UNKNOWN", 0.0, evidence


def check_employer(x: ExtractionResult, rules: RulesSnapshot) -> CheckResult:
    cfg = rules.config.employer
    emp_id = x.value("employer_id")
    emp_name = x.value("employer_name") or x.raw("employer_name")
    etype, conf, evidence = classify_employer(emp_id, emp_name, rules)
    # field confidence caps the classification confidence
    field_conf = max(x.confidence("employer_id"), x.confidence("employer_name"))
    conf = round(min(conf, field_conf) if field_conf else conf, 2)
    base = dict(check="EMPLOYER", rules_version=rules.version, evidence=evidence,
                details={"employer_id_masked": (emp_id[:4] + "******") if emp_id else None, "employer_name": emp_name, "type": etype})

    if etype == "INDIVIDUAL":
        if conf >= cfg.individual_auto_reject_threshold:
            return CheckResult(outcome="FAIL", label="INDIVIDUAL", confidence=conf, reason="Individual Employer", **base)
        return CheckResult(outcome="REVIEW", label="INDIVIDUAL", confidence=conf,
                           reason=f"Possible individual employer (confidence {conf:.2f})", **base)
    if etype in ("COMPANY", "GOVERNMENT"):
        return CheckResult(outcome="PASS", label=etype, confidence=conf, **base)
    return CheckResult(outcome="REVIEW", label="UNKNOWN", confidence=conf, reason="Employer classification unknown", **base)
