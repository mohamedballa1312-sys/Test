"""Persistence helpers shared by services: write/read ExtractionResult + Decision rows."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_encryptor
from app.db.models import DecisionRow, Document, ExtractedField
from app.engines.models import CheckResult, Decision, ExtractionResult, FieldValue


def save_extraction(s: Session, doc: Document, x: ExtractionResult) -> None:
    for f in doc.fields:
        f.is_current = False
    for fv in x.fields.values():
        b = fv.bbox or (None, None, None, None)
        s.add(ExtractedField(document_id=doc.id, field_name=fv.field, raw_text=fv.raw_text, normalized_value=fv.normalized,
                             confidence=fv.confidence, bbox_x=b[0], bbox_y=b[1], bbox_w=b[2], bbox_h=b[3],
                             source=fv.source, note=fv.note, corrected_by=fv.corrected_by, corrected_at=fv.corrected_at, is_current=True))
    doc.quality_score = x.quality_score
    doc.ocr_provider = x.ocr_provider
    doc.extraction_json = x.model_dump_json()
    iq = x.value("iqama_no")
    doc.iqama_no_hash = get_encryptor().keyed_hash(iq) if iq else None


def load_extraction(s: Session, doc: Document) -> ExtractionResult:
    """Current field set (with manual corrections applied) — what the engines run on."""
    base = ExtractionResult.model_validate_json(doc.extraction_json) if doc.extraction_json else ExtractionResult()
    base.fields = {}
    rows = s.execute(select(ExtractedField).where(ExtractedField.document_id == doc.id, ExtractedField.is_current == True)).scalars()  # noqa: E712
    for r in rows:
        bbox = (r.bbox_x, r.bbox_y, r.bbox_w, r.bbox_h) if r.bbox_x is not None else None
        base.fields[r.field_name] = FieldValue(field=r.field_name, raw_text=r.raw_text, normalized=r.normalized_value,
                                               confidence=r.confidence, bbox=bbox, source=r.source, note=r.note,
                                               corrected_by=r.corrected_by, corrected_at=r.corrected_at)
    base.quality_score = doc.quality_score or base.quality_score
    return base


def save_decision(s: Session, doc: Document, d: Decision, *, is_final: bool = False) -> DecisionRow:
    for old in doc.decisions:
        old.is_current = False
    row = DecisionRow(document_id=doc.id, version=d.version, status=d.status, reasons_json=json.dumps(d.reasons, ensure_ascii=False),
                      triggers_json=json.dumps(d.review_triggers, ensure_ascii=False), recommendation=d.recommendation,
                      checks_json=json.dumps([c.model_dump() for c in d.checks], ensure_ascii=False, default=str),
                      rules_version=d.rules_version, decided_by=d.decided_by, decided_at=d.decided_at, is_current=True, is_final=is_final)
    s.add(row)
    s.flush()
    return row


def current_decision(doc: Document) -> DecisionRow | None:
    cur = [d for d in doc.decisions if d.is_current]
    return cur[0] if cur else None


def decision_to_dict(row: DecisionRow | None) -> dict | None:
    if row is None:
        return None
    return {"id": row.id, "version": row.version, "status": row.status, "reasons": json.loads(row.reasons_json),
            "review_triggers": json.loads(row.triggers_json), "recommendation": row.recommendation,
            "checks": json.loads(row.checks_json or "[]"), "rules_version": row.rules_version,
            "decided_by": row.decided_by, "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "is_final": row.is_final}
