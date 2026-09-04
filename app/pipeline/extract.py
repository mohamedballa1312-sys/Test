"""Label-anchored field extraction (Architecture §3.3).

Card layout: `label: value` per row, label on the RIGHT, value to its LEFT; some rows carry two fields.
OCR may (a) merge label+value in one box, (b) split them, (c) merge two fields in one box,
(d) garble label characters. This module tolerates all four; it never assumes row count or position.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from rapidfuzz import fuzz

from app.core.text import has_arabic, has_latin, luhn_ok, normalize_arabic
from app.engines.models import ExtractionResult, FieldValue
from app.engines.rules import RulesSnapshot
from app.pipeline.normalize import clean_text, resolve_date, resolve_nationality, resolve_ten_digit
from app.pipeline.ocr.base import OCRLine, OCRProvider

LABEL_MIN_SCORE = 68          # fuzzy score (0..100) to accept a garbled label
PURE_LABEL_MIN_SCORE = 62     # a whole box that is only a label
RIGHT_COLUMN_LABEL_MIN = 58   # pure label sitting in the right-hand label column (x2 >= 0.9 W)
_SEP = re.compile(r"[:：;؛]")
NUMERIC_FIELDS = {"iqama_no": "2", "employer_id": "127"}
DATE_FIELDS = {"expiry_date", "birth_date"}
TEXT_FIELDS = {"birth_place", "nationality", "religion", "occupation", "issue_place", "work_place", "employer_name", "version_no"}


@dataclass
class Anchor:
    field: str
    line: OCRLine
    score: float                      # label match 0..100
    inline_value: str | None = None   # value found inside the same OCR box
    value_lines: list[OCRLine] = field(default_factory=list)


def _label_factor(score: float) -> float:
    return 0.8 + 0.2 * min(1.0, score / 100.0)


def _vertical_overlap(a: OCRLine, b: OCRLine) -> float:
    inter = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    return inter / max(1, min(a.h, b.h))


def merge_rows(lines: list[OCRLine], gap_factor: float = 1.2) -> list[OCRLine]:
    """Merge horizontally adjacent boxes on the same visual row (right->left) — OCR splits labels
    ("هوية" | "صاحب العمل:") and values across boxes. Keeps the parts for bbox recovery."""
    remaining = sorted(lines, key=lambda l: -l.x1)
    merged: list[OCRLine] = []
    used: set[int] = set()
    for i, a in enumerate(remaining):
        if i in used:
            continue
        group = [a]; used.add(i); cursor = a
        for j in range(i + 1, len(remaining)):
            if j in used:
                continue
            b = remaining[j]
            if _vertical_overlap(cursor, b) >= 0.45 and 0 <= (cursor.x1 - b.x2) <= gap_factor * min(cursor.h, b.h) * 1.0 or (
                _vertical_overlap(cursor, b) >= 0.45 and -0.3 * cursor.h <= (cursor.x1 - b.x2) < 0):
                group.append(b); used.add(j); cursor = b
        if len(group) == 1:
            merged.append(a)
        else:
            x1 = min(g.x1 for g in group); y1 = min(g.y1 for g in group)
            x2 = max(g.x2 for g in group); y2 = max(g.y2 for g in group)
            merged.append(OCRLine(text=" ".join(g.text for g in group), bbox=(x1, y1, x2 - x1, y2 - y1),
                                  confidence=float(np.mean([g.confidence for g in group])), parts=group))
    return merged


class Extractor:
    def __init__(self, rules: RulesSnapshot, provider: OCRProvider | None = None) -> None:
        self.rules = rules
        self.provider = provider
        self.W = 0
        self.labels: list[tuple[str, str]] = []  # (field, normalized label)
        for fld, variants in rules.card_labels.items():
            for v in variants:
                self.labels.append((fld, normalize_arabic(v).rstrip(":").strip()))

    def _in_label_zone(self, ln: OCRLine) -> bool:
        """All printed labels live in the right ~55% of the card; the QR caption (bottom-left) must never anchor."""
        return not self.W or ln.x2 >= 0.45 * self.W

    # ---------- label detection ----------
    def _best_label(self, text: str, *, anchored_start: bool, relaxed: bool = False) -> tuple[str, float, int, int] | None:
        """Best (field, score, start, end) for a label inside `text` (normalized)."""
        best = None
        for fld, lab in self.labels:
            if len(text) < 3:
                continue
            al = fuzz.partial_ratio_alignment(lab, text)
            if al is None:
                continue
            score = al.score
            # penalise matches that don't sit at the start when we expect a leading label
            if anchored_start and al.dest_start > 3:
                score -= 15
            # short labels (المهنة, الجنسية) need a stricter score to avoid matching inside values
            if len(lab) <= 7 and score < (LABEL_MIN_SCORE + 6 if not relaxed else RIGHT_COLUMN_LABEL_MIN):
                continue
            if best is None or score > best[1]:
                best = (fld, score, al.dest_start, al.dest_end)
        return best

    def _find_anchors(self, lines: list[OCRLine]) -> tuple[list[Anchor], list[OCRLine]]:
        anchors: list[Anchor] = []
        free: list[OCRLine] = []
        for ln in lines:
            norm = normalize_arabic(ln.text) or ""
            if not has_arabic(norm):
                free.append(ln); continue
            parts = [p.strip() for p in _SEP.split(norm)]
            found_any = False
            if len(parts) >= 2 and self._in_label_zone(ln):
                # "label: value [label2: value2]" — each part before a colon ends with a label
                in_col = self.W and ln.x2 >= 0.88 * self.W
                min_score = RIGHT_COLUMN_LABEL_MIN if in_col else LABEL_MIN_SCORE
                for i in range(len(parts) - 1):
                    head = parts[i]
                    # label is the suffix of `head`; the prefix (if any) is the previous field's value.
                    # The colon itself is evidence of a label, so the column prior may relax the score.
                    m = self._best_label(head, anchored_start=False, relaxed=bool(in_col))
                    if m and m[1] >= min_score:
                        fld, score, st, en = m
                        prev_value = head[:st].strip()
                        if anchors and found_any and prev_value:
                            anchors[-1].inline_value = prev_value
                        value = parts[i + 1]
                        # if the next part also contains a label, strip it from the value
                        if i + 1 < len(parts) - 1:
                            m2 = self._best_label(parts[i + 1], anchored_start=False)
                            if m2 and m2[1] >= LABEL_MIN_SCORE:
                                value = parts[i + 1][: m2[2]].strip()
                        a = Anchor(fld, ln, score, inline_value=value or None)
                        a.value_lines = _value_parts(ln, sum(len(x) + 1 for x in parts[: i + 1]))
                        anchors.append(a)
                        found_any = True
                if found_any:
                    continue
            # no usable colon: whole-box label? or "label value" without colon?
            m = self._best_label(norm, anchored_start=True, relaxed=True) if self._in_label_zone(ln) else None
            if m:
                fld, score, st, en = m
                lab_len = en - st
                rest = norm[en:].strip(" :;،.")
                in_label_column = self.W and ln.x2 >= 0.88 * self.W
                pure_min = RIGHT_COLUMN_LABEL_MIN if in_label_column else PURE_LABEL_MIN_SCORE
                if score >= LABEL_MIN_SCORE and st <= 2 and len(rest) >= 2:
                    a = Anchor(fld, ln, score, inline_value=rest)
                    a.value_lines = _value_parts(ln, en)
                    anchors.append(a); continue
                if score >= pure_min and lab_len >= 0.6 * len(norm):
                    anchors.append(Anchor(fld, ln, score)); continue
            free.append(ln)
        # keep the best-scoring anchor per field
        best: dict[str, Anchor] = {}
        for a in anchors:
            if a.field not in best or a.score > best[a.field].score or (a.inline_value and not best[a.field].inline_value and a.score >= best[a.field].score - 5):
                best[a.field] = a
        dropped = [a.line for a in anchors if best.get(a.field) is not a and a.line not in [b.line for b in best.values()]]
        free.extend(dropped)
        return list(best.values()), free

    # ---------- spatial value assignment ----------
    def _assign_values(self, anchors: list[Anchor], free: list[OCRLine]) -> None:
        anchor_lines = [a.line for a in anchors]
        for a in anchors:
            if a.inline_value:
                continue
            row = [l for l in free if _vertical_overlap(a.line, l) >= 0.4 and l.x2 <= a.line.x1 + 0.6 * a.line.h]
            # stop at the next anchor to the left on the same row
            left_anchors = [b.line for b in anchors if b is not a and _vertical_overlap(a.line, b.line) >= 0.4 and b.line.x2 <= a.line.x1 + 0.6 * a.line.h]
            if left_anchors:
                limit = max(l.x2 for l in left_anchors)
                row = [l for l in row if l.x1 >= limit - 0.3 * a.line.h]
            # values sit close to their label: drop boxes farther than ~45% of card width (uses anchor as scale)
            row.sort(key=lambda l: -l.x1)  # right -> left (Arabic reading order)
            picked: list[OCRLine] = []
            cursor = a.line.x1
            for l in row:
                gap = cursor - l.x2
                if gap > 6 * a.line.h:
                    break
                picked.append(l); cursor = l.x1
            a.value_lines = picked
            for l in picked:
                if l in free:
                    free.remove(l)

    # ---------- names ----------
    def _names(self, anchors: list[Anchor], free: list[OCRLine], W: int, H: int) -> tuple[FieldValue | None, FieldValue | None]:
        body = [a for a in anchors if a.field != "version_no"]
        first_y = min(a.line.y1 for a in body) if body else int(0.45 * H)
        cands = [l for l in free if l.y2 <= first_y + 4 and l.y1 >= 0.12 * H and l.x1 >= 0.30 * W and l.h >= 0.025 * H]
        cands = [l for l in cands if not any(fuzz.partial_ratio(normalize_arabic(l.text) or "", hi) >= 75 for hi in self.rules.header_ignore)]
        ar = sorted([l for l in cands if has_arabic(l.text) and not has_latin(l.text)], key=lambda l: -l.x1)
        en = sorted([l for l in cands if has_latin(l.text) and not has_arabic(l.text)], key=lambda l: l.x1)
        # keep the largest text row for each language (the name lines are the biggest under the header)
        def _row(group: list[OCRLine]) -> list[OCRLine]:
            if not group:
                return []
            tallest = max(group, key=lambda l: l.h)
            return sorted([l for l in group if _vertical_overlap(tallest, l) >= 0.4], key=(lambda l: -l.x1) if group is ar else (lambda l: l.x1))
        ar_row, en_row = _row(ar), _row(en)
        fa = fe = None
        if ar_row:
            txt = clean_text(" ".join(l.text for l in ar_row))
            fa = FieldValue(field="name_ar", raw_text=txt, normalized=txt, confidence=round(float(np.mean([l.confidence for l in ar_row])), 3), bbox=_union(ar_row), source="ocr")
        if en_row:
            txt = clean_text(" ".join(l.text for l in en_row))
            fe = FieldValue(field="name_en", raw_text=txt, normalized=txt.upper() if txt else None, confidence=round(float(np.mean([l.confidence for l in en_row])), 3), bbox=_union(en_row), source="ocr")
        return fa, fe

    # ---------- numeric refinement (second OCR pass, digits only) ----------
    def _digits_pass(self, image: np.ndarray, boxes: list[OCRLine], want_len: int = 10) -> tuple[str, float] | None:
        if self.provider is None or not boxes or image is None:
            return None
        x, y, w, h = _union(boxes)
        H, W = image.shape[:2]
        synthetic = all(b.text == "" for b in boxes)
        # values extend leftwards; pad left generously, right modestly (label sits on the right).
        # A synthetic (blind) box already ends just before the label: no horizontal padding.
        lpad, rpad = (0, 0) if synthetic else (int(1.5 * h), int(0.5 * h))
        crop = image[max(0, y - int(0.4 * h)):min(H, y + h + int(0.4 * h)), max(0, x - lpad):min(W, x + w + rpad)]
        if crop.size == 0:
            return None
        cands: list[tuple[int, float, str]] = []
        try:
            import cv2
            for fx in (2.0, 3.0):
                up = cv2.resize(crop, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)
                txt, conf = self.provider.read_digits(up)
                if not txt:
                    continue
                n = len(re.sub(r"\D", "", txt.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))))
                cands.append((abs(n - want_len), -conf, txt))
                if n == want_len:
                    break
        except Exception:
            pass
        if not cands:
            return None
        cands.sort()
        return cands[0][2], -cands[0][1]

    # ---------- main ----------
    def _blind_crop(self, image: np.ndarray, anchor: OCRLine, W: int, frac: float = 0.15) -> list[OCRLine]:
        """Detector missed the value box: synthesise a box left of the label to run the digits pass on."""
        x2 = anchor.x1 - 3
        x1 = max(0, anchor.x1 - int(frac * W))
        return [OCRLine(text="", bbox=(x1, anchor.y1, max(1, x2 - x1), anchor.h), confidence=0.0)]

    def extract(self, lines: list[OCRLine], image: np.ndarray | None, W: int, H: int) -> ExtractionResult:
        self.W = W
        res = ExtractionResult(raw_lines=[{"text": l.text, "bbox": list(l.bbox), "confidence": round(l.confidence, 3)} for l in lines])
        lines = [l for l in lines if not (l.confidence < 0.15 and len(l.text.strip()) <= 2)]
        lines = merge_rows(lines)
        anchors, free = self._find_anchors(lines)
        self._assign_values(anchors, free)
        res.anchors_found = len(anchors)
        by_field = {a.field: a for a in anchors}

        for fld, a in by_field.items():
            raw = a.inline_value if a.inline_value else clean_text(" ".join(l.text for l in a.value_lines))
            value_conf = float(np.mean([l.confidence for l in a.value_lines])) if a.value_lines else a.line.confidence
            conf = value_conf * _label_factor(a.score)
            bbox = _union(a.value_lines) if a.value_lines else a.line.bbox
            fv = FieldValue(field=fld, raw_text=raw, normalized=None, confidence=round(conf, 3), bbox=bbox, source="ocr")

            if fld in NUMERIC_FIELDS:
                num, mult, note = resolve_ten_digit(raw, NUMERIC_FIELDS[fld])
                # second pass: digits-only OCR on the value crop; visual order is natural order
                crop_boxes = a.value_lines or (self._blind_crop(image, a.line, W) if image is not None and not a.inline_value else [a.line])
                second = self._digits_pass(image, crop_boxes) if (mult < 1.0 or num is None or len(num or "") != 10) else None
                if second:
                    n2, m2, note2 = resolve_ten_digit(second[0], NUMERIC_FIELDS[fld])
                    if n2 and len(n2) == 10 and (m2 > mult or num is None or len(num) != 10):
                        num, mult, note = n2, max(m2, mult), f"digits_pass:{note2}"
                        value_conf = max(value_conf, second[1])
                fv.normalized = num if num and len(num) == 10 else None
                fv.confidence = round(value_conf * _label_factor(a.score) * (mult if fv.normalized else 0.3), 3)
                if fv.normalized and mult >= 1.0 and luhn_ok(fv.normalized) and fv.normalized[0] in NUMERIC_FIELDS[fld]:
                    # length + prefix + checksum are three independent agreements: strong enough to act on (D5)
                    fv.confidence = round(max(fv.confidence, 0.86), 3)
                fv.note = note
            elif fld in DATE_FIELDS:
                d, mult, note = resolve_date(raw)
                if (d is None or mult < 1.0) and image is not None:
                    crops: list[list[OCRLine]] = []
                    if a.value_lines:
                        crops.append(a.value_lines)
                    if not a.inline_value:
                        crops.append(self._blind_crop(image, a.line, W))
                        crops.append(self._blind_crop(image, a.line, W, frac=0.11))
                    for boxes in crops:
                        second = self._digits_pass(image, boxes, want_len=8)
                        if not second:
                            continue
                        d2, m2, note2 = resolve_date(second[0])
                        if d2 and (d is None or m2 > mult):
                            d, mult, note = d2, m2, f"digits_pass:{note2}"
                            value_conf = max(value_conf, second[1])
                        if d is not None and mult >= 1.0:
                            break
                fv.normalized = d.isoformat() if d else None
                fv.confidence = round(value_conf * _label_factor(a.score) * (mult if d else 0.0), 3)
                fv.note = note
            elif fld == "nationality":
                code, mult, note = resolve_nationality(raw, self.rules)
                fv.normalized = code
                # an exact hit in a closed vocabulary is strong evidence even when OCR confidence is modest
                fv.confidence = round(max(conf, 0.8) if note == "exact" else conf * (mult if code else 0.0), 3)
                fv.note = note
            elif fld == "version_no":
                digits = re.sub(r"\D", "", (raw or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
                fv.normalized = digits or None
            else:
                fv.normalized = clean_text(raw)
            res.set(fv)

        # ---- pattern fallbacks for the two IDs ----
        if res.value("iqama_no") is None or res.value("employer_id") is None:
            found: list[tuple[str, float, OCRLine]] = []
            for l in lines:
                for part in (l.parts or [l]):
                    n, mult, _ = resolve_ten_digit(part.text, "127")
                    if n and len(n) == 10:
                        found.append((n, part.confidence * mult * (1.0 if luhn_ok(n) else 0.7), part))
            if res.value("iqama_no") is None:
                c = [f for f in found if f[0][0] == "2"]
                if c:
                    n, cf, l = max(c, key=lambda t: t[1])
                    res.set(FieldValue(field="iqama_no", raw_text=l.text, normalized=n, confidence=round(max(cf * 0.95, 0.8 if luhn_ok(n) else 0.0), 3), bbox=l.bbox, source="pattern", note="pattern_fallback"))
                    res.warnings.append("iqama_no found by pattern, not by label")
            if res.value("employer_id") is None:
                c = [f for f in found if f[0][0] in "127" and f[0] != res.value("iqama_no")]
                if c:
                    n, cf, l = max(c, key=lambda t: t[1])
                    res.set(FieldValue(field="employer_id", raw_text=l.text, normalized=n, confidence=round(max(cf * 0.95, 0.78 if luhn_ok(n) else 0.0), 3), bbox=l.bbox, source="pattern", note="pattern_fallback"))
                    res.warnings.append("employer_id found by pattern, not by label")

        self._infer_by_row_order(res, anchors, free, W)
        fa, fe = self._names(anchors, free, W, H)
        if fa: res.set(fa)
        if fe: res.set(fe)
        return res

    # ---------- row-order inference ----------
    SINGLE_COLUMN_ORDER = ["nationality", "occupation", "employer_id", "issue_place", "work_place", "employer_name"]

    def _infer_by_row_order(self, res: ExtractionResult, anchors: list[Anchor], free: list[OCRLine], W: int) -> None:
        """Rows below the nationality row are single-field and printed in a fixed order. If exactly one
        field is missing between two found neighbours, a free Arabic line sitting between them is its value."""
        found = {a.field: a for a in anchors if a.field in self.SINGLE_COLUMN_ORDER}
        order = self.SINGLE_COLUMN_ORDER
        for i, fld in enumerate(order):
            if fld in found or fld in res.fields or fld == "work_place":
                continue
            above = next((found[f] for f in reversed(order[:i]) if f in found), None)
            below = next((found[f] for f in order[i + 1:] if f in found), None)
            if above is None or below is None:
                continue
            # exactly one missing between above and below?
            between = order[order.index(above.field) + 1: order.index(below.field)]
            if [f for f in between if f != "work_place" and f not in found] != [fld]:
                continue
            y_lo, y_hi = above.line.y2 - 2, below.line.y1 + 2
            cands = [l for l in free if l.y1 >= y_lo and l.y2 <= y_hi and has_arabic(l.text) and l.x1 >= 0.5 * W]
            # the garbled label itself sits in the label column; the value is the wider box left of it
            cands = [l for l in cands if not (l.x2 >= 0.88 * W and len(l.text.strip(" :;")) <= 12)]
            if not cands:
                continue
            l = max(cands, key=lambda l: l.bbox[2])
            raw = clean_text(l.text)
            if fld == "employer_id":
                continue  # numeric fields are handled by the pattern fallback
            fv = FieldValue(field=fld, raw_text=raw, normalized=raw, confidence=round(l.confidence * 0.85, 3),
                            bbox=l.bbox, source="ocr", note="row_order_inference")
            res.set(fv)
            free.remove(l)
            res.warnings.append(f"{fld}: label unreadable; value inferred from row order")


def _value_parts(line: OCRLine, label_end_offset: int) -> list[OCRLine]:
    """For a merged line, return the component boxes that lie after the label span (the value boxes)."""
    if not line.parts:
        return []
    offset = 0
    out: list[OCRLine] = []
    for p in line.parts:  # parts are stored right->left = logical order
        start, end = offset, offset + len(p.text)
        if start >= label_end_offset - 1:
            out.append(p)
        elif end > label_end_offset + 1 and len(p.text) > 0:
            # label ends inside this box: the value is its LEFT portion (RTL); approximate by character share
            frac = (end - label_end_offset) / len(p.text)
            x, y, w, h = p.bbox
            out.append(OCRLine(text=p.text, bbox=(x, y, max(1, int(w * frac)), h), confidence=p.confidence))
        offset = end + 1
    return out


def _union(boxes: list[OCRLine]) -> tuple[int, int, int, int]:
    x1 = min(b.x1 for b in boxes); y1 = min(b.y1 for b in boxes)
    x2 = max(b.x2 for b in boxes); y2 = max(b.y2 for b in boxes)
    return (x1, y1, x2 - x1, y2 - y1)
