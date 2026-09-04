"""EasyOCR provider (local, CPU). Models are fetched once from GitHub releases into ~/.EasyOCR."""
from __future__ import annotations

import numpy as np

from app.pipeline.ocr.base import OCRLine

_DIGITS = "0123456789٠١٢٣٤٥٦٧٨٩/"


class EasyOCRProvider:
    name = "easyocr"
    sends_data_externally = False

    def __init__(self, langs: tuple[str, ...] = ("ar", "en"), gpu: bool = False) -> None:
        import easyocr  # heavy import kept local

        self._reader = easyocr.Reader(list(langs), gpu=gpu, verbose=False)

    @staticmethod
    def _to_lines(results) -> list[OCRLine]:
        out: list[OCRLine] = []
        for box, text, conf in results:
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            x, y = int(min(xs)), int(min(ys))
            out.append(OCRLine(text=str(text), bbox=(x, y, int(max(xs) - x), int(max(ys) - y)), confidence=float(conf)))
        return out

    def read(self, image: np.ndarray) -> list[OCRLine]:
        return self._to_lines(self._reader.readtext(image, paragraph=False))

    def read_digits(self, crop: np.ndarray) -> tuple[str, float]:
        res = self._reader.readtext(crop, paragraph=False, allowlist=_DIGITS, detail=1)
        if not res:
            return "", 0.0
        # keep visual order left->right; extraction resolves chunk order
        res = sorted(res, key=lambda r: min(p[0] for p in r[0]))
        return " ".join(str(r[1]) for r in res), float(np.mean([r[2] for r in res]))
