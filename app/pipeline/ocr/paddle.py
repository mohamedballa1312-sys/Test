"""PaddleOCR provider (local, CPU) — primary provider for production per Architecture §3.2.
Models download on first use from Paddle's hosts; see README for offline placement."""
from __future__ import annotations

import numpy as np

from app.pipeline.ocr.base import OCRLine


class PaddleProvider:
    name = "paddle"
    sends_data_externally = False

    def __init__(self, lang: str = "ar") -> None:
        from paddleocr import PaddleOCR  # heavy import kept local

        self._ocr = PaddleOCR(lang=lang, use_doc_orientation_classify=False, use_doc_unwarping=False,
                              use_textline_orientation=False)

    def read(self, image: np.ndarray) -> list[OCRLine]:
        res = self._ocr.predict(image)
        out: list[OCRLine] = []
        if not res:
            return out
        r = res[0]
        for text, score, poly in zip(r["rec_texts"], r["rec_scores"], r["rec_polys"]):
            xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
            x, y = int(min(xs)), int(min(ys))
            out.append(OCRLine(text=str(text), bbox=(x, y, int(max(xs) - x), int(max(ys) - y)), confidence=float(score)))
        return out

    def read_digits(self, crop: np.ndarray) -> tuple[str, float]:
        lines = sorted(self.read(crop), key=lambda l: l.x1)
        if not lines:
            return "", 0.0
        return " ".join(l.text for l in lines), float(np.mean([l.confidence for l in lines]))
