"""Batch ingestion + background processing. Each document is isolated: one failure never stops the batch."""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import select

from app.audit.log import record
from app.core.clock import Clock
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import get_encryptor
from app.db.models import Batch, Document
from app.db.session import session_scope
from app.engines.decision import decide
from app.engines.rules import RulesRepository
from app.pipeline.ingest import IngestError, ingest
from app.pipeline.ocr.base import get_provider
from app.pipeline.runner import process_image
from app.services.company import resolve_company
from app.services.store import save_decision, save_extraction

log = get_logger("batch")


class BatchService:
    def __init__(self, rules: RulesRepository, clock: Clock | None = None, provider_name: str | None = None) -> None:
        self.rules = rules
        self.clock = clock
        self.settings = get_settings()
        self.provider_name = provider_name or self.settings.ocr_provider or rules.active.config.ocr.provider
        self._pool = ThreadPoolExecutor(max_workers=max(1, self.settings.workers))
        self._lock = threading.Lock()

    # ---------- ingestion ----------
    def create_batch(self, name: str, actor: str, requesting_companies: list[str] | None = None, project: dict | None = None) -> int:
        import json
        companies = [c.strip() for c in (requesting_companies or []) if c and c.strip()] or list(self.rules.active.config.permit.default_requesting_companies)
        with session_scope() as s:
            b = Batch(name=name, created_by=actor, rules_version=self.rules.active.version,
                      requesting_companies_json=json.dumps(companies, ensure_ascii=False), project_json=json.dumps(project or {}, ensure_ascii=False))
            s.add(b); s.flush()
            record(s, actor, "BATCH_CREATED", "batch", b.id, {"name": name, "requesting_companies": companies})
            return b.id

    def add_files(self, batch_id: int, files: list[tuple[str, bytes]], actor: str) -> dict:
        accepted: list[int] = []; rejected: list[dict] = []
        enc = get_encryptor()
        with session_scope() as s:
            b = s.get(Batch, batch_id)
            if b is None:
                raise KeyError(f"batch {batch_id} not found")
            for filename, data in files:
                try:
                    pages = ingest(data, filename)
                except IngestError as e:
                    rejected.append({"filename": filename, "error": str(e)})
                    record(s, actor, "UPLOAD_REJECTED", "batch", batch_id, {"filename": filename, "error": str(e)})
                    continue
                for p in pages:
                    dup = s.execute(select(Document.id).where(Document.sha256 == p.sha256)).scalar()
                    d = Document(batch_id=batch_id, original_filename=filename, sha256=p.sha256, mime=p.mime, page_no=p.page_no, duplicate_of=dup)
                    s.add(d); s.flush()
                    path = self.settings.images_dir / f"{d.id}.enc"
                    ok, buf = cv2.imencode(".png", p.image)
                    path.write_bytes(enc.encrypt_bytes(buf.tobytes()))
                    d.image_path = str(path)
                    accepted.append(d.id)
                    record(s, actor, "DOCUMENT_UPLOADED", "document", d.id, {"filename": filename, "page": p.page_no, "duplicate_of": dup})
            b.total = len(b.documents)
        return {"accepted": accepted, "rejected": rejected}

    # ---------- processing ----------
    def start_processing(self, batch_id: int, actor: str) -> None:
        with session_scope() as s:
            b = s.get(Batch, batch_id)
            if b is None:
                raise KeyError(f"batch {batch_id} not found")
            b.status = "PROCESSING"
            ids = [d.id for d in b.documents if d.status in ("QUEUED", "ERROR")]
            record(s, actor, "BATCH_PROCESS_STARTED", "batch", batch_id, {"documents": len(ids)})
        self._pool.submit(self._run_batch, batch_id, ids, actor)

    def process_document_sync(self, doc_id: int, actor: str = "system") -> None:
        self._process_one(doc_id, actor)

    def _run_batch(self, batch_id: int, ids: list[int], actor: str) -> None:
        # OCR models are not thread-safe in every backend: process sequentially inside the worker
        for doc_id in ids:
            self._process_one(doc_id, actor)
            with session_scope() as s:
                b = s.get(Batch, batch_id)
                b.processed = sum(1 for d in b.documents if d.status in ("DONE", "ERROR"))
        with session_scope() as s:
            b = s.get(Batch, batch_id)
            b.status = "DONE"
            record(s, actor, "BATCH_PROCESS_DONE", "batch", batch_id, {"processed": b.processed, "total": b.total})
        self._apply_retention(batch_id)

    def load_image(self, doc: Document) -> np.ndarray | None:
        if not doc.image_path or not Path(doc.image_path).exists():
            return None
        raw = get_encryptor().decrypt_bytes(Path(doc.image_path).read_bytes())
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)

    def _process_one(self, doc_id: int, actor: str) -> None:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            doc.status = "PROCESSING"
            img = self.load_image(doc)
        try:
            if img is None:
                raise RuntimeError("image missing")
            provider = get_provider(self.provider_name)
            rules = self.rules.active
            x, card = process_image(img, rules, provider, self.clock)
            d = decide(x, rules, self.clock)
            with session_scope() as s:
                doc = s.get(Document, doc_id)
                save_extraction(s, doc, x)
                import json
                doc.doc_type = x.doc_type
                if doc.company_source != "MANUAL":
                    name, source, _ = resolve_company(x.value("employer_name"), json.loads(doc.batch.requesting_companies_json or "[]"), x.doc_type, rules)
                    doc.company_final, doc.company_source = name, source
                # keep the processed card (what bboxes refer to) for the review screen
                ok, buf = cv2.imencode(".png", card)
                Path(doc.image_path).write_bytes(get_encryptor().encrypt_bytes(buf.tobytes()))
                save_decision(s, doc, d)
                doc.status = "DONE"; doc.error_msg = None
                doc.processed_at = datetime.now(timezone.utc)
                record(s, actor, "DOCUMENT_PROCESSED", "document", doc_id,
                       {"status": d.status, "quality": x.quality_score, "reasons": d.reasons, "triggers": d.review_triggers})
            log.info("processed", doc_id=doc_id, status=d.status)
        except Exception as e:  # isolate failures
            log.error("document failed", doc_id=doc_id, error=str(e))
            with session_scope() as s:
                doc = s.get(Document, doc_id)
                doc.status = "ERROR"; doc.error_msg = f"{type(e).__name__}: {e}"[:1000]
                record(s, actor, "DOCUMENT_FAILED", "document", doc_id, {"error": str(e)[:300]})
            traceback.print_exc()

    # ---------- retention ----------
    def _apply_retention(self, batch_id: int) -> None:
        cfg = self.rules.active.config.retention
        if not cfg.delete_images_after_final_decision or cfg.delete_images_after == "PERMIT_EXPORT":
            return  # images are needed for the permit request; deleted on export
        with session_scope() as s:
            b = s.get(Batch, batch_id)
            for d in b.documents:
                cur = [x for x in d.decisions if x.is_current]
                # images are kept while a human still needs them (MANUAL_REVIEW); deleted once final
                if cur and (cur[0].status != "MANUAL_REVIEW" or cur[0].is_final):
                    delete_image(s, d, "system")


def delete_image(s, doc: Document, actor: str) -> None:
    if doc.image_path and Path(doc.image_path).exists():
        Path(doc.image_path).unlink()
    if doc.image_path:
        doc.image_path = None
        doc.image_deleted_at = datetime.now(timezone.utc)
        record(s, actor, "IMAGE_DELETED", "document", doc.id, {})
