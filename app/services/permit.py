"""Permit-request generator: fills the customer's Word template (Tarshid "طلب تصريح") in place.

The template is treated as data (uploaded by the user, stored under data/templates, never in the repo):
letterhead, footer and stamp stay byte-identical; only the slots below are written.

Slots (after run-merging the template once on upload):
  - date paragraph:  'تاريخ الاصدار)'  -> Gregorian ; '(تاريخ الاصدار هجري)' -> Hijri (Umm al-Qura)
  - table 0 (project): 4 rows, cell 0 = value
  - tables 1+2 (team): 27 numbered rows; extra rows are cloned; approved rows get the stamp in the notes cell
  - tables 3+4 (images): rebuilt as a 2-column grid of card images (+stamp) with captions
"""
from __future__ import annotations

import html
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from PIL import Image

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
EMU_PER_CM = 360000


@dataclass
class Worker:
    name: str
    nationality: str
    company: str
    id_number: str
    approved: bool = True
    note: str = ""
    image_png: bytes | None = None


@dataclass
class PermitData:
    issue_date: date
    project_name: str = ""
    project_location: str = ""
    work_start: str = ""
    work_end_expected: str = ""
    workers: list[Worker] = field(default_factory=list)
    stamp_approved_rows: bool = True
    stamp_images: bool = True


def hijri_str(d: date) -> str:
    try:
        from hijridate import Gregorian
        h = Gregorian(d.year, d.month, d.day).to_hijri()
        return f"{h.year:04d}/{h.month:02d}/{h.day:02d} هـ"
    except Exception:
        return ""


# ---------------------------------------------------------------- XML helpers
_P = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_TBL = re.compile(r"<w:tbl>.*?</w:tbl>", re.S)
_TR = re.compile(r"<w:tr[ >].*?</w:tr>", re.S)
_TC = re.compile(r"<w:tc>.*?</w:tc>", re.S)
_RUN = re.compile(r"<w:r[ >].*?</w:r>|<w:r>.*?</w:r>", re.S)


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _cell_text(tc: str) -> str:
    return html.unescape("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", tc))).strip()


def _first_rpr(xml: str) -> str:
    m = re.search(r"<w:rPr>.*?</w:rPr>", xml, re.S)
    return m.group(0) if m else ""


def _set_paragraph_text(p: str, text: str, rpr: str, keep_drawings: bool = False) -> str:
    """Replace the runs of a paragraph with one text run (optionally keeping runs that carry drawings)."""
    kept = [r for r in _RUN.findall(p) if keep_drawings and "<w:drawing" in r]
    body_start = p.find(">") + 1
    ppr = re.match(r"<w:pPr>.*?</w:pPr>", p[body_start:], re.S)
    ppr_xml = ppr.group(0) if ppr else ""
    open_tag = p[:body_start]
    run = f'<w:r>{rpr}<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>' if text else ""
    return f"{open_tag}{ppr_xml}{''.join(kept)}{run}</w:p>"


def _set_cell(tc: str, text: str, rpr: str, keep_drawings: bool = False) -> str:
    tc = re.sub(r"<w:p\s*/>", "<w:p></w:p>", tc)   # self-closing empty paragraphs
    ps = _P.findall(tc)
    if not ps:
        return tc
    first = ps[0]
    new_first = _set_paragraph_text(first, text, rpr, keep_drawings)
    out = tc.replace(first, new_first, 1)
    # drop the other paragraphs' text (placeholders spanning lines) but keep drawings
    for extra in ps[1:]:
        out = out.replace(extra, _set_paragraph_text(extra, "", rpr, keep_drawings), 1)
    return out


def _renumber_docpr(xml: str, start: int) -> tuple[str, int]:
    n = start
    def rep(m):
        nonlocal n
        n += 1
        return f'<wp:docPr id="{n}"'
    return re.sub(r'<wp:docPr id="\d+"', rep, xml), n


def _inline_image_xml(rid: str, cx: int, cy: int, docpr_id: int, name: str) -> str:
    return (
        f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{docpr_id}" name="{_esc(name)}"/>'
        f'<wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="0" name="{_esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        f'</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )


class PermitTemplateError(ValueError):
    pass


# ---------------------------------------------------------------- generator
class PermitGenerator:
    def __init__(self, template_bytes: bytes) -> None:
        self.template = template_bytes
        try:
            zf = zipfile.ZipFile(io.BytesIO(template_bytes))
        except zipfile.BadZipFile as e:
            raise PermitTemplateError("not a .docx file") from e
        with zf as z:
            names = set(z.namelist())
            if "word/document.xml" not in names:
                raise PermitTemplateError("not a .docx (no word/document.xml)")
            self.doc_xml = z.read("word/document.xml").decode("utf-8")
            self.rels_xml = z.read("word/_rels/document.xml.rels").decode("utf-8")
        body = self.doc_xml.split("<w:body>", 1)[1]
        self.tables = _TBL.findall(body)
        if len(self.tables) < 5:
            raise PermitTemplateError(f"expected 5 tables in the permit template, found {len(self.tables)}")

    # ---- slots ----
    def _fill_dates(self, xml: str, d: date) -> str:
        g = d.strftime("%Y/%m/%d")
        xml = xml.replace("تاريخ الاصدار)", f"{g})", 1)
        xml = xml.replace("(تاريخ الاصدار هجري)", f"({hijri_str(d)})", 1)
        return xml

    def _fill_project(self, xml: str, data: PermitData) -> str:
        t0 = self.tables[0]
        rows = _TR.findall(t0)
        values = [data.project_name, data.project_location, data.work_start, data.work_end_expected]
        new_t0 = t0
        for row, val in zip(rows, values):
            cells = _TC.findall(row)
            rpr = _first_rpr(cells[0]) or _first_rpr(cells[1])
            new_row = row.replace(cells[0], _set_cell(cells[0], val, rpr), 1)
            new_t0 = new_t0.replace(row, new_row, 1)
        return xml.replace(t0, new_t0, 1)

    def _fill_team(self, xml: str, data: PermitData, docpr_start: int) -> tuple[str, int]:
        t1, t2 = self.tables[1], self.tables[2]
        rows1, rows2 = _TR.findall(t1), _TR.findall(t2)
        header, body_rows = rows1[0], rows1[1:] + rows2
        # stamp drawing + text style come from the first data row
        first_cells = _TC.findall(body_rows[0])
        stamp_run = next((r for r in _RUN.findall(first_cells[0]) if "<w:drawing" in r), "")
        rpr = _first_rpr(first_cells[4]) or _first_rpr(first_cells[1])
        # extra rows: clone the last row pattern
        row_template = rows2[-1]
        workers = data.workers
        all_rows = list(body_rows)
        n_extra = max(0, len(workers) - len(all_rows))
        extra_rows = []
        for k in range(n_extra):
            extra_rows.append(row_template)
        docpr = docpr_start

        def build(row_xml: str, idx: int, w: Worker | None) -> str:
            cells = _TC.findall(row_xml)
            new = row_xml
            vals = {5: str(idx + 1)}
            if w:
                vals.update({4: w.name, 3: w.nationality, 2: w.company, 1: w.id_number, 0: w.note})
            for ci, cell in enumerate(cells):
                text = vals.get(ci, "")
                cell_rpr = _first_rpr(cell) or rpr
                if ci == 0:
                    # notes cell: drop template drawings, add the stamp for approved workers
                    nc = _set_cell(cell, text, cell_rpr, keep_drawings=False)
                    if w and w.approved and data.stamp_approved_rows and stamp_run:
                        nonlocal docpr
                        stamped, docpr = _renumber_docpr(stamp_run, docpr)
                        nc = nc.replace("</w:p>", stamped + "</w:p>", 1)
                    new = new.replace(cell, nc, 1)
                else:
                    new = new.replace(cell, _set_cell(cell, text, cell_rpr), 1)
            return new

        filled1 = [build(r, i, workers[i] if i < len(workers) else None) for i, r in enumerate(body_rows[:len(rows1) - 1])]
        offset = len(rows1) - 1
        filled2 = [build(r, offset + i, workers[offset + i] if offset + i < len(workers) else None) for i, r in enumerate(rows2)]
        filled2 += [build(r, len(all_rows) + k, workers[len(all_rows) + k]) for k, r in enumerate(extra_rows)]
        new_t1 = t1.replace("".join(rows1), header + "".join(filled1), 1)
        new_t2 = t2.replace("".join(rows2), "".join(filled2), 1)
        xml = xml.replace(t1, new_t1, 1).replace(t2, new_t2, 1)
        return xml, docpr

    def _build_images(self, xml: str, data: PermitData, docpr_start: int) -> tuple[str, int, dict[str, bytes], list[tuple[str, str]]]:
        t3, t4 = self.tables[3], self.tables[4]
        rows4 = _TR.findall(t4)
        row_tpl = rows4[0]
        cells_tpl = _TC.findall(row_tpl)
        stamp_run = next((r for r in _RUN.findall(t3) if "<w:drawing" in r), "")
        grid = re.findall(r'<w:gridCol w:w="(\d+)"', t4)
        col_twips = int(grid[0]) if grid else 4500
        max_w_emu = int(min(col_twips / 20 * 0.0352778, 8.0) * EMU_PER_CM * 0.92)  # cell width in EMU with margin
        media: dict[str, bytes] = {}
        rels: list[tuple[str, str]] = []
        docpr = docpr_start
        images = [w for w in data.workers if w.image_png and w.approved]
        rows_out = []
        rpr = _first_rpr(t4) or _first_rpr(self.tables[1])
        for i in range(0, len(images), 2):
            pair = images[i:i + 2]
            cells_out = []
            for ci, tc in enumerate(cells_tpl):
                # template columns are right-to-left in reading order; fill cell 1 first then cell 0
                k = 1 - ci
                if k < len(pair):
                    w = pair[k]
                    im = Image.open(io.BytesIO(w.image_png)).convert("RGB")
                    if im.width > 1000:
                        im = im.resize((1000, int(im.height * 1000 / im.width)))
                    buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
                    ratio = im.height / max(1, im.width)
                    cx = max_w_emu; cy = int(cx * ratio)
                    n = len(media) + 1
                    fname = f"media/iqama_{n}.jpg"; rid = f"rIdIqama{n}"
                    media[f"word/{fname}"] = buf.getvalue()
                    rels.append((rid, fname))
                    docpr += 1
                    img_run = _inline_image_xml(rid, cx, cy, docpr, f"iqama {n}")
                    stamp = ""
                    if data.stamp_images and stamp_run:
                        stamp, docpr = _renumber_docpr(stamp_run, docpr)
                    p_img = f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr>{img_run}{stamp}</w:p>'
                    p_cap = f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r>{rpr}<w:t xml:space="preserve">{_esc(w.name)}</w:t></w:r></w:p>'
                    tcpr = re.match(r"<w:tc><w:tcPr>.*?</w:tcPr>", tc, re.S)
                    cells_out.append((tcpr.group(0) if tcpr else "<w:tc>") + p_img + p_cap + "</w:tc>")
                else:
                    cells_out.append(_set_cell(tc, "", rpr))
            trpr = re.match(r"<w:tr[^>]*>(<w:trPr>.*?</w:trPr>)?", row_tpl, re.S)
            rows_out.append((trpr.group(0) if trpr else "<w:tr>") + "".join(cells_out) + "</w:tr>")
        new_t4 = t4.replace("".join(rows4), "".join(rows_out) if rows_out else "".join(_set_cell(c, "", rpr) for c in [row_tpl]), 1)
        xml = xml.replace(t3, "", 1).replace(t4, new_t4, 1)
        return xml, docpr, media, rels

    # ---- public ----
    def render_docx(self, data: PermitData) -> bytes:
        xml = self.doc_xml
        ids = [int(x) for x in re.findall(r'<wp:docPr id="(\d+)"', xml)]
        docpr = max(ids or [100]) + 1000
        xml = self._fill_dates(xml, data.issue_date)
        xml = self._fill_project(xml, data)
        xml, docpr = self._fill_team(xml, data, docpr)
        xml, docpr, media, rels = self._build_images(xml, data, docpr)
        rels_xml = self.rels_xml
        for rid, target in rels:
            rels_xml = rels_xml.replace("</Relationships>", f'<Relationship Id="{rid}" Type="{REL_IMAGE}" Target="{target}"/></Relationships>', 1)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.template)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "word/document.xml":
                    zout.writestr(item, xml.encode("utf-8"))
                elif item.filename == "word/_rels/document.xml.rels":
                    zout.writestr(item, rels_xml.encode("utf-8"))
                else:
                    zout.writestr(item, zin.read(item.filename))
            for name, blob in media.items():
                zout.writestr(name, blob)
        return out.getvalue()

    @staticmethod
    def to_pdf(docx_bytes: bytes, timeout: int = 120) -> bytes | None:
        """LibreOffice conversion; None when soffice is unavailable or fails (caller falls back to .docx)."""
        exe = shutil.which("soffice") or shutil.which("libreoffice")
        if not exe:
            return None
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "permit.docx"; src.write_bytes(docx_bytes)
            try:
                subprocess.run([exe, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", td, str(src)],
                               capture_output=True, timeout=timeout, check=False)
            except Exception:
                return None
            pdf = Path(td) / "permit.pdf"
            return pdf.read_bytes() if pdf.exists() else None
