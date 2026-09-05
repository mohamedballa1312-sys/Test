"""Builds the permit request (Word/PDF) for a batch from the stored template and reviewed results."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import cv2

from app.audit.log import record
from app.core.clock import Clock
from app.core.config import get_settings
from app.db.models import Batch
from app.db.session import session_scope
from app.engines.rules import RulesRepository
from app.services.batch import BatchService, delete_image
from app.services.permit import PermitData, PermitGenerator, PermitTemplateError, Worker
from app.services.store import current_decision, load_extraction

TEMPLATE_NAME = "permit_request.docx"


def template_path() -> Path:
    d = get_settings().data_dir / "templates"
    d.mkdir(parents=True, exist_ok=True)
    return d / TEMPLATE_NAME


def save_template(data: bytes, actor: str) -> dict:
    """Validate, merge fragmented runs (so slot texts are contiguous) and store the template."""
    PermitGenerator(data)  # raises PermitTemplateError when the structure is not the expected form
    merged = _merge_runs(data)
    PermitGenerator(merged)
    template_path().write_bytes(merged)
    with session_scope() as s:
        record(s, actor, "PERMIT_TEMPLATE_UPLOADED", "template", TEMPLATE_NAME, {"bytes": len(merged)})
    return {"template": TEMPLATE_NAME, "bytes": len(merged)}


def _merge_runs(data: bytes) -> bytes:
    """Coalesce adjacent identically-formatted runs in word/document.xml (Word splits placeholders into fragments)."""
    import io, re, zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
        others = {n: z.read(n) for n in z.namelist() if n != "word/document.xml"}
    run_re = re.compile(r"<w:r>(<w:rPr>.*?</w:rPr>)?<w:t(?: xml:space=\"preserve\")?>([^<]*)</w:t></w:r>", re.S)
    def merge_para(p: str) -> str:
        parts = list(run_re.finditer(p))
        if len(parts) < 2:
            return p
        out = []; i = 0
        while i < len(parts):
            j = i; text = parts[i].group(2); rpr = parts[i].group(1) or ""
            while j + 1 < len(parts) and (parts[j + 1].group(1) or "") == rpr and parts[j + 1].start() == parts[j].end():
                j += 1; text += parts[j].group(2)
            out.append((parts[i].start(), parts[j].end(), f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'))
            i = j + 1
        res = p; 
        for a, b, rep in reversed(out):
            res = res[:a] + rep + res[b:]
        return res
    xml = re.sub(r"<w:p[ >].*?</w:p>", lambda m: merge_para(m.group(0)), xml, flags=re.S)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, b in others.items():
            zout.writestr(n, b)
        zout.writestr("word/document.xml", xml.encode("utf-8"))
    return buf.getvalue()


class PermitExportService:
    def __init__(self, rules: RulesRepository, batches: BatchService, clock: Clock | None = None) -> None:
        self.rules = rules
        self.batches = batches
        self.clock = clock or Clock()

    def build_data(self, batch_id: int) -> tuple[PermitData, list[int]]:
        cfg = self.rules.active.config.permit
        rules = self.rules.active
        with session_scope() as s:
            b = s.get(Batch, batch_id)
            if b is None:
                raise KeyError(f"batch {batch_id} not found")
            project = json.loads(b.project_json or "{}")
            workers: list[Worker] = []; ids: list[int] = []
            for d in sorted(b.documents, key=lambda d: d.id):
                dec = current_decision(d)
                if dec is None or dec.status not in cfg.include_statuses:
                    continue
                x = load_extraction(s, d)
                nat = x.value("nationality")
                row = rules.nationality_by_code(nat) if nat else None
                img = self.batches.load_image(d)
                png = cv2.imencode(".png", img)[1].tobytes() if img is not None else None
                workers.append(Worker(name=x.value("name_ar") or x.value("name_en") or "", nationality=(row.name_ar if row else (nat or "")),
                                      company=d.company_final or x.value("employer_name") or "", id_number=x.value("iqama_no") or "",
                                      approved=dec.status == "APPROVED", note="", image_png=png))
                ids.append(d.id)
            data = PermitData(issue_date=self.clock.today(), project_name=project.get("name", ""), project_location=project.get("location", ""),
                              work_start=project.get("work_start", ""), work_end_expected=project.get("work_end_expected", ""),
                              workers=workers, stamp_approved_rows=cfg.stamp_approved_rows, stamp_images=cfg.stamp_images)
            return data, ids

    def export(self, batch_id: int, fmt: str, actor: str) -> tuple[bytes, str, list[str]]:
        tp = template_path()
        if not tp.exists():
            raise PermitTemplateError("no permit template uploaded yet (PUT /api/v1/templates/permit)")
        gen = PermitGenerator(tp.read_bytes())
        data, ids = self.build_data(batch_id)
        warnings: list[str] = []
        docx = gen.render_docx(data)
        out, mime = docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if fmt == "pdf":
            pdf = PermitGenerator.to_pdf(docx)
            if pdf:
                out, mime = pdf, "application/pdf"
            else:
                warnings.append("PDF conversion unavailable (LibreOffice not found or failed); returned .docx instead")
        with session_scope() as s:
            b = s.get(Batch, batch_id)
            b.permit_exported_at = datetime.now(timezone.utc)
            record(s, actor, "PERMIT_EXPORTED", "batch", batch_id, {"format": fmt, "workers": len(data.workers), "warnings": warnings})
            cfg = self.rules.active.config.retention
            if cfg.delete_images_after_final_decision and cfg.delete_images_after == "PERMIT_EXPORT":
                for d in b.documents:
                    dec = current_decision(d)
                    if dec is not None and dec.is_final:
                        delete_image(s, d, actor)
        return out, mime, warnings
