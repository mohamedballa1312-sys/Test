from __future__ import annotations

import io
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from app.api import schemas
from app.api.deps import actor, batch_service, clock, report_service, require_api_key, review_service, rules_repo
from app.audit.log import record
from app.core.security import mask_id
from app.db.models import AuditLog, Batch, DecisionRow, Document
from app.db.session import session_scope
from app.engines.rules import FILES, RulesLoadError
from app.services import retention
from app.services.store import current_decision, decision_to_dict, load_extraction

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
SENSITIVE = {"iqama_no", "employer_id"}


def _batch_out(b: Batch, summary: dict | None = None) -> dict:
    return {"id": b.id, "name": b.name, "status": b.status, "total": b.total, "processed": b.processed,
            "created_at": b.created_at.isoformat(), "rules_version": b.rules_version, "summary": summary}


def _doc_out(s, d: Document, unmask: bool) -> dict:
    x = load_extraction(s, d)
    fields = {}
    for name, fv in x.fields.items():
        val = fv.normalized
        raw = fv.raw_text
        if name in SENSITIVE and not unmask:
            val, raw = mask_id(val), mask_id(raw) if raw and raw.strip().isdigit() else raw
        fields[name] = {"field": name, "raw_text": raw, "normalized": val, "confidence": fv.confidence,
                        "bbox": list(fv.bbox) if fv.bbox else None, "source": fv.source, "note": fv.note, "corrected_by": fv.corrected_by}
    dec = decision_to_dict(current_decision(d))
    if dec and not unmask:
        for c in dec.get("checks", []):
            c.get("details", {}).pop("employer_id", None)
    return {"id": d.id, "batch_id": d.batch_id, "filename": d.original_filename, "page_no": d.page_no, "status": d.status,
            "error": d.error_msg, "quality_score": d.quality_score, "has_image": bool(d.image_path), "duplicate_of": d.duplicate_of,
            "fields": fields, "decision": dec, "warnings": x.warnings}


# ---------------- batches ----------------
@router.post("/batches", response_model=schemas.BatchOut, status_code=201)
def create_batch(body: schemas.BatchCreate, who: str = Depends(actor)):
    bid = batch_service().create_batch(body.name, who)
    with session_scope() as s:
        return _batch_out(s.get(Batch, bid))


@router.get("/batches")
def list_batches(limit: int = Query(50, le=500)):
    with session_scope() as s:
        rows = s.execute(select(Batch).order_by(Batch.id.desc()).limit(limit)).scalars().all()
        return [_batch_out(b) for b in rows]


@router.post("/batches/{batch_id}/documents", response_model=schemas.UploadOut)
async def upload(batch_id: int, files: list[UploadFile] = File(...), who: str = Depends(actor)):
    payload = [(f.filename or "upload", await f.read()) for f in files]
    try:
        return batch_service().add_files(batch_id, payload, who)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/batches/{batch_id}/process", status_code=202)
def process(batch_id: int, who: str = Depends(actor)):
    try:
        batch_service().start_processing(batch_id, who)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"batch_id": batch_id, "status": "PROCESSING"}


@router.get("/batches/{batch_id}", response_model=schemas.BatchOut)
def get_batch(batch_id: int):
    with session_scope() as s:
        b = s.get(Batch, batch_id)
        if b is None:
            raise HTTPException(404, "batch not found")
        out = _batch_out(b)
    out["summary"] = report_service().summary(batch_id)
    return out


@router.get("/batches/{batch_id}/documents")
def list_documents(batch_id: int, decision: str | None = None, trigger: str | None = None, q: str | None = None,
                   unmask: bool = False, who: str = Depends(actor)):
    with session_scope() as s:
        b = s.get(Batch, batch_id)
        if b is None:
            raise HTTPException(404, "batch not found")
        out = []
        for d in sorted(b.documents, key=lambda d: d.id):
            o = _doc_out(s, d, unmask)
            dec = o["decision"] or {}
            if decision and dec.get("status") != decision and not (decision == "ERROR" and d.status == "ERROR"):
                continue
            if trigger:
                hay = " ".join(dec.get("review_triggers", []) + dec.get("reasons", []))
                if trigger.lower() not in hay.lower():
                    continue
            if q:
                hay = json.dumps(o["fields"], ensure_ascii=False) + o["filename"]
                if q.lower() not in hay.lower():
                    continue
            o.pop("warnings", None)
            out.append(o)
        if unmask:
            record(s, who, "PII_UNMASKED_LIST", "batch", batch_id, {"count": len(out)})
        return out


@router.get("/batches/{batch_id}/export")
def export(batch_id: int, format: str = Query("xlsx", pattern="^(xlsx|csv)$"), unmask: bool = False, who: str = Depends(actor)):
    try:
        if format == "csv":
            data = report_service().to_csv(batch_id, unmask, who)
            return Response(data, media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="permit_file_{batch_id}.csv"'})
        data = report_service().to_xlsx(batch_id, unmask, who)
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="permit_file_{batch_id}.xlsx"'})
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.delete("/batches/{batch_id}", status_code=204)
def delete_batch(batch_id: int, who: str = Depends(actor)):
    if not retention.delete_batch(batch_id, who):
        raise HTTPException(404, "batch not found")
    return Response(status_code=204)


# ---------------- documents ----------------
@router.get("/documents/{doc_id}", response_model=schemas.DocumentOut)
def get_document(doc_id: int, unmask: bool = False, who: str = Depends(actor)):
    with session_scope() as s:
        d = s.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "document not found")
        if unmask:
            record(s, who, "PII_UNMASKED", "document", doc_id, {})
        return _doc_out(s, d, unmask)


@router.get("/documents/{doc_id}/image")
def get_image(doc_id: int, who: str = Depends(actor)):
    with session_scope() as s:
        d = s.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "document not found")
        img = batch_service().load_image(d)
        if img is None:
            raise HTTPException(410, "image deleted per retention policy")
        record(s, who, "IMAGE_VIEWED", "document", doc_id, {})
    import cv2
    ok, buf = cv2.imencode(".png", img)
    return StreamingResponse(io.BytesIO(buf.tobytes()), media_type="image/png")


@router.patch("/documents/{doc_id}/fields")
def correct(doc_id: int, body: schemas.Corrections, who: str = Depends(actor)):
    try:
        return review_service().correct_fields(doc_id, body.fields, who)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/documents/{doc_id}/review")
def submit_review(doc_id: int, body: schemas.ReviewSubmit, who: str = Depends(actor)):
    try:
        return review_service().submit(doc_id, body.status, body.note, who)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/documents/{doc_id}/reprocess", status_code=202)
def reprocess(doc_id: int, who: str = Depends(actor)):
    with session_scope() as s:
        d = s.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "document not found")
        if not d.image_path:
            raise HTTPException(410, "image deleted; cannot reprocess")
    batch_service()._pool.submit(batch_service().process_document_sync, doc_id, who)
    return {"document_id": doc_id, "status": "PROCESSING"}


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, who: str = Depends(actor)):
    if not retention.delete_document(doc_id, who):
        raise HTTPException(404, "document not found")
    return Response(status_code=204)


# ---------------- review queue ----------------
@router.get("/review/queue")
def review_queue(batch_id: int | None = None):
    return review_service().queue(batch_id)


# ---------------- rules ----------------
@router.get("/rules")
def get_rules():
    snap = rules_repo().active
    return {"version": snap.version, "loaded_at": snap.loaded_at.isoformat(), "config": snap.config.model_dump(),
            "occupations": [o.model_dump() for o in snap.occupations],
            "nationalities": [n.model_dump() for n in snap.nationalities],
            "files": list(FILES)}


@router.get("/rules/files/{name}")
def get_rules_file(name: str):
    snap = rules_repo().active
    if name not in snap.files:
        raise HTTPException(404, "unknown rules file")
    return {"name": name, "content": snap.files[name], "version": snap.version}


@router.put("/rules/files/{name}")
def put_rules_file(name: str, body: schemas.RulesFile, who: str = Depends(actor)):
    try:
        snap = rules_repo().replace_file(name, body.content, who)
    except RulesLoadError as e:
        raise HTTPException(422, f"rules rejected, previous version kept active: {e}")
    with session_scope() as s:
        record(s, who, "RULES_UPDATED", "rules", name, {"version": snap.version})
    return {"version": snap.version}


@router.post("/rules/reload")
def reload_rules(who: str = Depends(actor)):
    try:
        snap = rules_repo().reload(who)
    except RulesLoadError as e:
        raise HTTPException(422, f"rules rejected, previous version kept active: {e}")
    with session_scope() as s:
        record(s, who, "RULES_RELOADED", "rules", None, {"version": snap.version})
    return {"version": snap.version}


@router.get("/rules/versions")
def rules_versions():
    return [{"version": v, "loaded_at": t.isoformat()} for v, t in rules_repo().history]


@router.post("/rules/occupations", status_code=201)
def upsert_occupation(body: schemas.OccupationUpsert, who: str = Depends(actor)):
    try:
        snap = rules_repo().upsert_occupation(body.model_dump(), who)
    except RulesLoadError as e:
        raise HTTPException(422, str(e))
    with session_scope() as s:
        record(s, who, "OCCUPATION_UPSERTED", "rules", body.occupation_ar, {"eligible": body.eligible, "version": snap.version})
    return {"version": snap.version, "count": len(snap.occupations)}


# ---------------- audit ----------------
@router.get("/audit")
def audit(limit: int = Query(200, le=2000), action: str | None = None, actor_filter: str | None = Query(None, alias="actor"),
          since: datetime | None = None):
    with session_scope() as s:
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if action:
            q = q.where(AuditLog.action == action)
        if actor_filter:
            q = q.where(AuditLog.actor == actor_filter)
        if since:
            q = q.where(AuditLog.ts >= since)
        return [{"id": a.id, "ts": a.ts.isoformat(), "actor": a.actor, "action": a.action, "entity_type": a.entity_type,
                 "entity_id": a.entity_id, "details": json.loads(a.details_json)} for a in s.execute(q).scalars()]
