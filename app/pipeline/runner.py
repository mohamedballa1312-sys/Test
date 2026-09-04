"""End-to-end: image -> ExtractionResult. Retries with a 180° rotation when the card seems upside down."""
from __future__ import annotations

import cv2
import numpy as np

from app.core.clock import Clock
from app.engines.models import ExtractionResult
from app.engines.rules import RulesSnapshot
from app.pipeline.extract import Extractor
from app.pipeline.ocr.base import OCRProvider
from app.pipeline.preprocess import preprocess
from app.pipeline.validate import validate

MIN_ANCHORS_BEFORE_ROTATE = 3


def process_image(img: np.ndarray, rules: RulesSnapshot, provider: OCRProvider, clock: Clock | None = None,
                  *, keep_raw_lines: bool = True) -> tuple[ExtractionResult, np.ndarray]:
    """Returns (extraction, processed card image)."""
    cfg = rules.config
    pre = preprocess(img, upscale_below_px=cfg.image.upscale_below_px)
    card = pre.image
    res = _run(card, rules, provider)
    if res.anchors_found < MIN_ANCHORS_BEFORE_ROTATE:
        rotated = cv2.rotate(card, cv2.ROTATE_180)
        res2 = _run(rotated, rules, provider)
        if res2.anchors_found > res.anchors_found:
            res, card = res2, rotated
            res.warnings.append("card was upside down; rotated 180°")
    res.quality_score = pre.quality_score
    res.ocr_provider = provider.name
    res.card_size = pre.card_size
    if not pre.card_found:
        res.warnings.append("card boundary not detected; used full image")
    validate(res, rules, (clock or Clock(cfg.expiry.timezone)).today())
    if not keep_raw_lines:
        res.raw_lines = []
    return res, card


def _run(card: np.ndarray, rules: RulesSnapshot, provider: OCRProvider) -> ExtractionResult:
    lines = provider.read(card)
    H, W = card.shape[:2]
    return Extractor(rules, provider).extract(lines, card, W, H)
