"""File acceptance: real type detection, size limits, PDF page splitting, duplicate hashing."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps

MAX_FILE_MB = 25
ALLOWED = {"image/jpeg", "image/png", "application/pdf"}


class IngestError(ValueError):
    pass


@dataclass
class IngestedPage:
    image: np.ndarray  # BGR
    page_no: int
    sha256: str
    mime: str


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return "application/octet-stream"


def _pil_to_bgr(img: Image.Image) -> np.ndarray:
    img = ImageOps.exif_transpose(img).convert("RGB")
    return np.asarray(img)[:, :, ::-1].copy()


def ingest(data: bytes, filename: str = "") -> list[IngestedPage]:
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise IngestError(f"file too large (> {MAX_FILE_MB} MB): {filename}")
    mime = sniff_mime(data)
    if mime not in ALLOWED:
        raise IngestError(f"unsupported or corrupt file (detected {mime}): {filename}")
    sha = hashlib.sha256(data).hexdigest()
    if mime == "application/pdf":
        try:
            import fitz  # pymupdf
        except ImportError as e:  # pragma: no cover
            raise IngestError("PDF support requires the 'pymupdf' package") from e
        pages: list[IngestedPage] = []
        doc = fitz.open(stream=data, filetype="pdf")
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=200)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(IngestedPage(_pil_to_bgr(img), i, hashlib.sha256(sha.encode() + str(i).encode()).hexdigest(), mime))
        if not pages:
            raise IngestError(f"PDF has no pages: {filename}")
        return pages
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise IngestError(f"cannot decode image: {filename}") from e
    return [IngestedPage(_pil_to_bgr(img), 1, sha, mime)]
