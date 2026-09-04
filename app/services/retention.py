"""Deletion (right to erasure) and time-based retention."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.audit.log import record
from app.db.models import Batch, Document
from app.db.session import session_scope
from app.engines.rules import RulesRepository
from app.services.batch import delete_image


def delete_document(doc_id: int, actor: str) -> bool:
    with session_scope() as s:
        d = s.get(Document, doc_id)
        if d is None:
            return False
        delete_image(s, d, actor)
        s.delete(d)
        record(s, actor, "DOCUMENT_DELETED", "document", doc_id, {})
        return True


def delete_batch(batch_id: int, actor: str) -> bool:
    with session_scope() as s:
        b = s.get(Batch, batch_id)
        if b is None:
            return False
        for d in list(b.documents):
            delete_image(s, d, actor)
        s.delete(b)
        record(s, actor, "BATCH_DELETED", "batch", batch_id, {})
        return True


def purge_expired(rules: RulesRepository, actor: str = "system") -> int:
    days = rules.active.config.retention.data_retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    with session_scope() as s:
        for b in s.query(Batch).filter(Batch.created_at < cutoff).all():
            for d in list(b.documents):
                delete_image(s, d, actor)
            s.delete(b); n += 1
        if n:
            record(s, actor, "RETENTION_PURGE", "batch", None, {"deleted_batches": n, "cutoff": cutoff.isoformat()})
    return n
