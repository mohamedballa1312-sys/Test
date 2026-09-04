"""Post-extraction validation: checksum signals, date sanity, completeness. Adjusts confidence, never decides."""
from __future__ import annotations

from datetime import date

from app.core.text import luhn_ok
from app.engines.models import ExtractionResult
from app.engines.rules import RulesSnapshot


def validate(x: ExtractionResult, rules: RulesSnapshot, today: date | None = None) -> ExtractionResult:
    for fld in ("iqama_no", "employer_id"):
        fv = x.fields.get(fld)
        if fv and fv.normalized:
            if len(fv.normalized) != 10 or not fv.normalized.isdigit():
                x.warnings.append(f"{fld}: not 10 digits")
                fv.confidence = round(fv.confidence * 0.5, 3)
            elif not luhn_ok(fv.normalized):
                x.warnings.append(f"{fld}: checksum failed (advisory)")
                fv.confidence = round(fv.confidence * 0.8, 3)
                fv.note = (fv.note or "") + " luhn_fail"
    iq = x.fields.get("iqama_no")
    if iq and iq.normalized and iq.normalized[0] != "2":
        x.warnings.append("iqama_no: does not start with 2")
        iq.confidence = round(iq.confidence * 0.5, 3)
    b, e = x.date_value("birth_date"), x.date_value("expiry_date")
    if b and e and b >= e:
        x.warnings.append("birth_date >= expiry_date")
        x.fields["expiry_date"].confidence = round(x.fields["expiry_date"].confidence * 0.7, 3)
    if b:
        lo, hi = 1920, (today or date.today()).year - 14
        if not (lo <= b.year <= hi):
            x.warnings.append("birth_date out of plausible range")
            x.fields["birth_date"].confidence = round(x.fields["birth_date"].confidence * 0.5, 3)
    if e and not (2000 <= e.year <= 2100):
        x.warnings.append("expiry_date out of plausible range")
        x.fields["expiry_date"].confidence = round(x.fields["expiry_date"].confidence * 0.3, 3)
    for f in rules.config.ocr.critical_fields:
        if x.value(f) in (None, ""):
            x.warnings.append(f"missing critical field: {f}")
    return x
