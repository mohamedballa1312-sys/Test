"""Card detection/crop, perspective + orientation correction, enhancement, quality score."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class PreprocessResult:
    image: np.ndarray            # BGR, card-cropped, enhanced, upscaled
    quality_score: float         # 0..1
    card_found: bool
    original_size: tuple[int, int]
    card_size: tuple[int, int]
    scale: float                 # applied upscale factor
    metrics: dict


def _order_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1); d = np.diff(pts, axis=1).ravel()
    rect[0] = pts[np.argmin(s)]; rect[2] = pts[np.argmax(s)]
    rect[1] = pts[np.argmin(d)]; rect[3] = pts[np.argmax(d)]
    return rect


def detect_card(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """Find the largest light, roughly rectangular region (the card) and crop/warp to it.
    Falls back to the full image. Screenshots keep UI chrome outside the card — this removes it."""
    h, w = img.shape[:2]
    area = h * w
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    # cards are bright on darker/neutral backgrounds; combine edges + brightness threshold
    edges = cv2.Canny(blur, 40, 120)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
    _, bright = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.bitwise_or(edges, bright)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None; best_area = 0
    for c in contours:
        a = cv2.contourArea(c)
        if a < 0.12 * area or a > 0.985 * area:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        ratio = bw / max(bh, 1)
        if not (1.2 <= ratio <= 2.2):  # ID-1 card ≈ 1.586; allow phone crops
            continue
        if a > best_area:
            best, best_area = c, a
    if best is None:
        return img, False
    peri = cv2.arcLength(best, True)
    approx = cv2.approxPolyDP(best, 0.02 * peri, True)
    if len(approx) == 4:
        rect = _order_points(approx.reshape(4, 2).astype("float32"))
        (tl, tr, br, bl) = rect
        wA = np.linalg.norm(br - bl); wB = np.linalg.norm(tr - tl)
        hA = np.linalg.norm(tr - br); hB = np.linalg.norm(tl - bl)
        W, H = int(max(wA, wB)), int(max(hA, hB))
        if W > 50 and H > 50:
            dst = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], dtype="float32")
            M = cv2.getPerspectiveTransform(rect, dst)
            return cv2.warpPerspective(img, M, (W, H)), True
    x, y, bw, bh = cv2.boundingRect(best)
    pad = int(0.01 * max(bw, bh))
    return img[max(0, y - pad):min(h, y + bh + pad), max(0, x - pad):min(w, x + bw + pad)], True


def enhance(img: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    return cv2.fastNlMeansDenoisingColored(out, None, 3, 3, 7, 21)


def quality_metrics(img: np.ndarray) -> dict:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    glare = float((gray >= 254).mean())  # saturated pixels; white card stock sits ~215-235
    return {"width": w, "height": h, "sharpness": sharp, "brightness": mean, "glare_ratio": glare}


def quality_score(m: dict) -> float:
    """0..1. Calibrated so a readable ~700px phone screenshot scores ≈0.6 and a 300px one fails."""
    w = m["width"]
    res = 0.0 if w < 400 else min(1.0, (w - 400) / 800.0)    # 0 below 400px, 0.375 at 700px, 1.0 at 1200px
    sharp = min(1.0, m["sharpness"] / 150.0)                 # laplacian variance; blurry <50
    bright = 1.0 - min(1.0, abs(m["brightness"] - 170) / 170.0)
    glare = 1.0 - min(1.0, m["glare_ratio"] / 0.30)
    score = 0.55 * res + 0.25 * sharp + 0.10 * bright + 0.10 * glare
    if w < 600:
        score = min(score, 0.40)                             # too small to trust regardless of sharpness
    return round(float(score), 3)


def preprocess(img: np.ndarray, upscale_below_px: int = 1200, target_width: int = 1600) -> PreprocessResult:
    oh, ow = img.shape[:2]
    if oh > ow * 1.15:  # portrait phone capture of a landscape card
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    card, found = detect_card(img)
    m = quality_metrics(card)
    score = quality_score(m)
    ch, cw = card.shape[:2]
    scale = 1.0
    if cw < upscale_below_px:
        scale = target_width / cw
        card = cv2.resize(card, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    card = enhance(card)
    return PreprocessResult(image=card, quality_score=score, card_found=found, original_size=(ow, oh),
                            card_size=(card.shape[1], card.shape[0]), scale=scale, metrics=m)
