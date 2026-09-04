"""Manual review: queue, field correction (re-runs engines only), final sign-off."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.audit.log import record
from app.core.clock import Clock
from app.db.models import DecisionRow, Document, ExtractedField, Review
from app.db.session import session_scope
from app.engines.decision import decide
from app.engines.models import FieldValue
from app.engines.rules import RulesRepository
from app.pipeline.normalize import resolve_nationality
from app.pipeline.validate import validate
from app.services.batch import delete_image
from app.services.store import current_decision, decision_to_dict, load_extraction, save_decision
from app.core.text import parse_date


class ReviewService:
    def __init__(self, rules: RulesRepository, clock: Clock | None = None) -> None:
        self.rules = rules
        self.clock = clock

    def queue(self, batch_id: int | None = None) -> list[dict]:
        with session_scope() as s:
            q = select(Document).join(DecisionRow).where(DecisionRow.is_current == True, DecisionRow.status == "MANUAL_REVIEW", DecisionRow.is_final == False)  # noqa: E712
            if batch_id:
                q = q.where(Document.batch_id == batch_id)
            out = []
            for d in s.execute(q).scalars().unique():
                dec = current_decision(d)
                out.append({"document_id": d.id, "batch_id": d.batch_id, "filename": d.original_filename,
                            "recommendation": dec.recommendation, "triggers": decision_to_dict(dec)["review_triggers"]})
            return out

    def correct_fields(self, doc_id: int, corrections: dict[str, str | None], actor: str) -> dict:
        """Apply human corrections, then re-run engines + decision on the corrected values (no OCR)."""
        rules = self.rules.active
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                raise KeyError(f"document {doc_id} not found")
            now = datetime.now(timezone.utc)
            applied = {}
            for name, value in corrections.items():
                normalized = _normalize_manual(name, value, rules)
                for f in doc.fields:
                    if f.field_name == name and f.is_current:
                        f.is_current = False
                s.add(ExtractedField(document_id=doc.id, field_name=name, raw_text=value, normalized_value=normalized,
                                     confidence=1.0, source="manual", corrected_by=actor, corrected_at=now, is_current=True))
                applied[name] = normalized
            s.flush()
            x = load_extraction(s, doc)
            validate(x, rules, (self.clock or Clock()).today())
            prev = current_decision(doc)
            version = (prev.version + 1) if prev else 1
            d = decide(x, rules, self.clock, decided_by=actor, version=version)
            row = save_decision(s, doc, d)
            record(s, actor, "FIELDS_CORRECTED", "document", doc_id, {"fields": list(applied), "new_status": d.status, "version": version})
            return {"document_id": doc_id, "applied": applied, "decision": decision_to_dict(row)}

    def submit(self, doc_id: int, final_status: str, note: str | None, actor: str) -> dict:
        if final_status not in ("APPROVED", "REJECTED"):
            raise ValueError("final_status must be APPROVED or REJECTED")
        rules = self.rules.active
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                raise KeyError(f"document {doc_id} not found")
            prev = current_decision(doc)
            x = load_extraction(s, doc)
            d = decide(x, rules, self.clock, decided_by=actor, version=(prev.version + 1) if prev else 1)
            # the human's verdict overrides the engine's; reasons kept for the report
            d.status = final_status  # type: ignore[assignment]
            if final_status == "APPROVED":
                d.reasons = []
            elif not d.reasons:
                d.reasons = [f"Rejected by reviewer{': ' + note if note else ''}"]
            row = save_decision(s, doc, d, is_final=True)
            s.add(Review(document_id=doc_id, reviewer=actor, submitted_at=datetime.now(timezone.utc), final_status=final_status,
                         note=note, previous_decision_id=prev.id if prev else None, new_decision_id=row.id))
            record(s, actor, "REVIEW_SUBMITTED", "document", doc_id, {"final_status": final_status, "note": note})
            if rules.config.retention.delete_images_after_final_decision:
                delete_image(s, doc, actor)
            return {"document_id": doc_id, "decision": decision_to_dict(row)}


def _normalize_manual(name: str, value: str | None, rules) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip()
    if name in ("expiry_date", "birth_date"):
        d = parse_date(v)
        if d is None:
            raise ValueError(f"{name}: cannot parse date '{v}' (use YYYY-MM-DD)")
        return d.isoformat()
    if name in ("iqama_no", "employer_id"):
        digits = "".join(ch for ch in v.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")) if ch.isdigit())
        if len(digits) != 10:
            raise ValueError(f"{name}: must be 10 digits")
        return digits
    if name == "nationality":
        code, _, _ = resolve_nationality(v, rules)
        if code is None:
            raise ValueError(f"nationality: '{v}' not in nationalities.csv")
        return code
    return v
