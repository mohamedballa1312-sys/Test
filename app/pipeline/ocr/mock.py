"""Deterministic provider for tests: returns canned lines keyed by an image tag."""
from __future__ import annotations

import numpy as np

from app.pipeline.ocr.base import OCRLine


class MockProvider:
    name = "mock"
    sends_data_externally = False

    def __init__(self, lines: list[OCRLine] | None = None, digits: dict[str, tuple[str, float]] | None = None) -> None:
        self.lines = lines or []
        self.digits = digits or {}
        self.calls = 0

    def read(self, image: np.ndarray) -> list[OCRLine]:
        self.calls += 1
        return list(self.lines)

    def read_digits(self, crop: np.ndarray) -> tuple[str, float]:
        return "", 0.0
