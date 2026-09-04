"""OCRProvider interface. Everything above this line is provider-agnostic."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass
class OCRLine:
    text: str
    bbox: tuple[int, int, int, int]  # x, y, w, h
    confidence: float
    parts: list["OCRLine"] | None = None  # set when this line was merged from adjacent boxes

    @property
    def x1(self) -> int: return self.bbox[0]
    @property
    def y1(self) -> int: return self.bbox[1]
    @property
    def x2(self) -> int: return self.bbox[0] + self.bbox[2]
    @property
    def y2(self) -> int: return self.bbox[1] + self.bbox[3]
    @property
    def cy(self) -> float: return self.bbox[1] + self.bbox[3] / 2
    @property
    def h(self) -> int: return self.bbox[3]


@runtime_checkable
class OCRProvider(Protocol):
    name: str
    sends_data_externally: bool

    def read(self, image: np.ndarray) -> list[OCRLine]: ...

    def read_digits(self, crop: np.ndarray) -> tuple[str, float]:
        """Recognise a crop known to contain only digits and separators. Providers without a
        character allowlist fall back to read() and join the text."""
        ...


_REGISTRY: dict[str, str] = {
    "paddle": "app.pipeline.ocr.paddle:PaddleProvider",
    "easyocr": "app.pipeline.ocr.easyocr_provider:EasyOCRProvider",
    "mock": "app.pipeline.ocr.mock:MockProvider",
}
_instances: dict[str, OCRProvider] = {}


def get_provider(name: str, **kwargs) -> OCRProvider:
    """Lazy, cached construction — OCR models are expensive to load."""
    if name in _instances:
        return _instances[name]
    if name not in _REGISTRY:
        raise ValueError(f"unknown OCR provider '{name}'. Known: {sorted(_REGISTRY)}")
    mod_name, cls_name = _REGISTRY[name].split(":")
    import importlib
    cls = getattr(importlib.import_module(mod_name), cls_name)
    inst = cls(**kwargs)
    _instances[name] = inst
    return inst


def register_provider(name: str, instance: OCRProvider) -> None:
    _instances[name] = instance
