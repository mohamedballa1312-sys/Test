"""Label-anchored field extraction (Architecture §3.3).

Card layout: `label: value` per row, label on the RIGHT, value to its LEFT; some rows carry two fields.
OCR may (a) merge label+value in one box, (b) split them, (c) merge two fields in one box,
(d) garble label characters. This module tolerates all four; it never assumes row count or position.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

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
    ltr: bool = False                 # English label: the value sits to its RIGHT


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
        self.H = 0
        self.secondary: dict[str, Anchor] = {}
        self.labels: list[tuple[str, str]] = []        # (field, normalized Arabic label)
        self.latin_labels: list[tuple[str, str]] = []  # (field, lowercase English label) - matched by word boundary
        for fld, variants in rules.card_labels.items():
            for v in variants:
                if has_latin(v) and not has_arabic(v):
                    self.latin_labels.append((fld, v.lower().strip()))
                else:
                    self.labels.append((fld, normalize_arabic(v).rstrip(":").strip()))

    def _in_label_zone(self, ln: OCRLine) -> bool:
        """All printed labels live in the right ~55% of the card; the QR caption (bottom-left) must never anchor."""
        return not self.W or ln.x2 >= 0.35 * self.W

    def _latin_anchor(self, ln: OCRLine) -> Anchor | None:
        """English labels on national ID cards ("ID Number:", "Date of Birth:", "DOB:"): exact word match,
        value follows the label in the same box or sits in the next box to the right."""
        t = (ln.text or "").strip()
        low = t.lower()
        for fld, lab in sorted(self.latin_labels, key=lambda x: -len(x[1])):
            words = [re.escape(w) for w in lab.split()]
            pat = r"(?<![a-z])" + r"\s*".join(words) + r"(?![a-z])\s*[:.]?\s*"
            m = re.search(pat, low)
            if m is None and len(lab) >= 8:
                # OCR-garbled long label ("Expiry Dale:"): fuzzy on the head of the line
                head = low[: len(lab) + 3]
                al = fuzz.partial_ratio_alignment(lab, head)
                if al and al.score >= 80 and al.dest_start <= 2:
                    m = re.compile(r".{" + str(al.dest_end) + r"}\s*[:.]?\s*").match(low)
            if m and m.start() <= 2:
                rest = t[m.end():].strip(" :.")
                a = Anchor(fld, ln, 100.0 + 0.01 * len(lab), inline_value=rest or None, ltr=True)
                if ln.parts and rest:
                    a.value_lines = [p for p in ln.parts if p.x1 >= ln.parts[0].x1 and re.search(r"\d", p.text)] or []
                return a
        return None

    # ---------- label detection ----------
    def _best_label(self, text: str, *, anchored_start: bool, relaxed: bool = False) -> tuple[str, float, int, int] | None:
        """Best (field, score, start, end) for a label inside `text` (normalized)."""
        best = None
        best_key = None
        for fld, lab in self.labels:
            if len(text) < 3:
                continue
            al = fuzz.partial_ratio_alignment(lab, text)
            if al is None:
                continue
            score = al.score
            # short labels (المهنة, الجنسية, الرقم) need a stricter score to avoid matching inside values
            if len(lab) <= 7 and score < (LABEL_MIN_SCORE + 6 if not relaxed else RIGHT_COLUMN_LABEL_MIN):
                continue
            # a bare substring label ("صاحب العمل", "الميلاد") must not beat its longer form: longer wins ties
            score += 0.01 * len(lab)
            # a label sits at the START of a line (leading) or at the END of a pre-colon head (suffix);
            # a match in the right position beats a slightly higher score in the wrong position
            well_placed = (al.dest_start <= 2) if anchored_start else (al.dest_end >= len(text) - 2)
            key = (well_placed, score)
            if best_key is None or key > best_key:
                best, best_key = (fld, score, al.dest_start, al.dest_end), key
        return best

    def _find_anchors(self, lines: list[OCRLine]) -> tuple[list[Anchor], list[OCRLine]]:
        anchors: list[Anchor] = []
        free: list[OCRLine] = []
        for ln in lines:
            norm = normalize_arabic(ln.text) or ""
            if not has_arabic(norm):
                la = self._latin_anchor(ln)
                if la:
                    anchors.append(la)
                else:
                    free.append(ln)
                continue
            # header words ("هوية مقيم", "وزارة الداخلية") and anything in the top band are never field labels
            if (self.H and ln.y2 < 0.12 * self.H) or any(fuzz.ratio(norm, hi) >= 80 for hi in self.rules.header_ignore):
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
            m = self._best_label(norm, anchored_start=True, relaxed=bool(self.W and ln.x2 >= 0.88 * self.W)) if self._in_label_zone(ln) else None
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
        # keep the best-scoring anchor per field; an English (Gregorian) date label beats the Arabic (Hijri) one,
        # and the runner-up for dates/IDs is kept for cross-checking
        self.secondary: dict[str, Anchor] = {}
        best: dict[str, Anchor] = {}
        for a in anchors:
            cur = best.get(a.field)
            better = cur is None or a.ltr and not cur.ltr or (a.ltr == cur.ltr and (a.score > cur.score or (a.inline_value and not cur.inline_value and a.score >= cur.score - 5)))
            if better:
                if cur is not None and a.field in DATE_FIELDS | set(NUMERIC_FIELDS):
                    self.secondary[a.field] = cur
                best[a.field] = a
            elif a.field in DATE_FIELDS | set(NUMERIC_FIELDS) and a.field not in self.secondary:
                self.secondary[a.field] = a
        kept_lines = [b.line for b in best.values()] + [b.line for b in self.secondary.values()]
        free.extend(a.line for a in anchors if a.line not in kept_lines)
        return list(best.values()), free

    # ---------- spatial value assignment ----------
    def _assign_values(self, anchors: list[Anchor], free: list[OCRLine]) -> None:
        anchor_lines = [a.line for a in anchors]
        for a in anchors:
            if a.inline_value:
                continue
            if a.ltr:
                row = sorted([l for l in free if _vertical_overlap(a.line, l) >= 0.4 and l.x1 >= a.line.x2 - 0.6 * a.line.h], key=lambda l: l.x1)
                picked = []
                cursor = a.line.x2
                for l in row:
                    if l.x1 - cursor > 6 * a.line.h or not re.search(r"\d", l.text):
                        break
                    picked.append(l); cursor = l.x2
                a.value_lines = picked
                for l in picked:
                    if l in free:
                        free.remove(l)
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

    # ---------- text refinement (second OCR pass on the value crop, magnified) ----------
    TEXT_PASS_FIELDS = {"occupation", "employer_name", "nationality", "birth_place", "work_place", "issue_place"}
    TEXT_PASS_BELOW = 0.75

    def _text_pass(self, image: np.ndarray, boxes: list[OCRLine]) -> tuple[str, float] | None:
        """Re-read a value region at 2.5x: small Arabic words on screenshots gain a lot from magnification."""
        if self.provider is None or not boxes or image is None:
            return None
        x, y, w, h = _union(boxes)
        H, W = image.shape[:2]
        crop = image[max(0, y - int(0.5 * h)):min(H, y + h + int(0.5 * h)), max(0, x - int(0.6 * h)):min(W, x + w + int(0.6 * h))]
        if crop.size == 0:
            return None
        try:
            import cv2
            up = cv2.resize(crop, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
            lines = [l for l in self.provider.read(up) if has_arabic(l.text)]
        except Exception:
            return None
        if not lines:
            return None
        lines.sort(key=lambda l: -l.x1)   # right -> left reading order
        text = clean_text(" ".join(l.text for l in lines))
        conf = float(np.mean([l.confidence for l in lines]))
        return (text, conf) if text else None

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
    def _blind_crop(self, image: np.ndarray, anchor: OCRLine, W: int, frac: float = 0.15, ext: float = 1.0) -> list[OCRLine]:
        """Detector missed (or swallowed) the value box: synthesise a box left of the label for the digits pass.
        The label's own box frequently overlaps the value's last digits, so extend `ext` label-heights into it."""
        label_x1 = anchor.x1
        if anchor.parts:
            arabic = [p for p in anchor.parts if has_arabic(p.text) and not re.fullmatch(r"[\s٠-٩0-9/.:-]+", p.text)]
            if arabic:
                label_x1 = min(p.x1 for p in arabic)
        x2 = min(anchor.x2, label_x1 + int(ext * anchor.h))
        x1 = max(0, label_x1 - int(frac * W))
        return [OCRLine(text="", bbox=(x1, anchor.y1, max(1, x2 - x1), anchor.h), confidence=0.0)]

    def extract(self, lines: list[OCRLine], image: np.ndarray | None, W: int, H: int) -> ExtractionResult:
        self.W, self.H = W, H
        res = ExtractionResult(raw_lines=[{"text": l.text, "bbox": list(l.bbox), "confidence": round(l.confidence, 3)} for l in lines])
        lines = [l for l in lines if not (l.confidence < 0.15 and len(l.text.strip()) <= 2)]
        lines = merge_rows(lines)
        is_national = self._doc_type_cues(lines) >= 1
        allowed_prefix = {"iqama_no": "1" if is_national else "2", "employer_id": "127"}
        anchors, free = self._find_anchors(lines)
        self._assign_values(anchors + list(self.secondary.values()), free)
        res.anchors_found = len(anchors)
        by_field = {a.field: a for a in anchors}

        for fld, a in by_field.items():
            raw = a.inline_value if a.inline_value else clean_text(" ".join(l.text for l in a.value_lines))
            value_conf = float(np.mean([l.confidence for l in a.value_lines])) if a.value_lines else a.line.confidence
            conf = value_conf * _label_factor(a.score)
            bbox = _union(a.value_lines) if a.value_lines else a.line.bbox
            fv = FieldValue(field=fld, raw_text=raw, normalized=None, confidence=round(conf, 3), bbox=bbox, source="ocr")

            if fld in NUMERIC_FIELDS:
                num, mult, note = resolve_ten_digit(raw, allowed_prefix[fld])
                # second pass: digits-only OCR on the value crop; visual order is natural order
                crop_boxes = a.value_lines or (self._blind_crop(image, a.line, W) if image is not None and not a.inline_value else [a.line])
                second = self._digits_pass(image, crop_boxes) if (mult < 1.0 or num is None or len(num or "") != 10) else None
                if second:
                    n2, m2, note2 = resolve_ten_digit(second[0], allowed_prefix[fld])
                    if n2 and len(n2) == 10 and (m2 > mult or num is None or len(num) != 10):
                        num, mult, note = n2, max(m2, mult), f"digits_pass:{note2}"
                        value_conf = max(value_conf, second[1])
                fv.normalized = num if num and len(num) == 10 else None
                fv.confidence = round(value_conf * _label_factor(a.score) * (mult if fv.normalized else 0.3), 3)
                if fv.normalized and mult >= 1.0 and luhn_ok(fv.normalized) and fv.normalized[0] in allowed_prefix[fld]:
                    # length + prefix + checksum are three independent agreements: strong enough to act on (D5)
                    fv.confidence = round(max(fv.confidence, 0.86), 3)
                fv.note = note
            elif fld in DATE_FIELDS:
                d, mult, note = resolve_date(raw)
                if (d is None or mult < 1.0) and image is not None:
                    crops: list[list[OCRLine]] = []
                    if a.value_lines:
                        crops.append(a.value_lines)
                    if not a.inline_value or len(re.sub(r"\D", "", a.inline_value.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))) < 8:
                        crops.append(self._blind_crop(image, a.line, W, ext=0.0))
                        crops.append(self._blind_crop(image, a.line, W, ext=0.4))
                        crops.append(self._blind_crop(image, a.line, W, frac=0.11, ext=0.2))
                    # a crop that includes label strokes can yield a plausible-but-wrong 8-digit date:
                    # only a separator-bearing ("direct") parse may stop the search; otherwise keep the best multiplier
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
                # cross-check against the other script's copy of the same date (national ID: Hijri vs Gregorian)
                sec = self.secondary.get(fld)
                if d is not None and sec is not None:
                    sraw = sec.inline_value or clean_text(" ".join(l.text for l in sec.value_lines))
                    d_sec, m_sec, _ = resolve_date(sraw)
                    if d_sec is not None:
                        if abs((d_sec - d).days) <= 2:
                            fv.confidence = round(max(fv.confidence, 0.9), 3); fv.note = (fv.note or "") + " xcheck_ok"
                        else:
                            fv.confidence = round(fv.confidence * 0.6, 3); fv.note = (fv.note or "") + f" xcheck_mismatch({d_sec.isoformat()})"
                            res.warnings.append(f"{fld}: Hijri/Gregorian copies disagree ({d.isoformat()} vs {d_sec.isoformat()})")
            elif fld == "nationality":
                if conf < self.TEXT_PASS_BELOW and image is not None and (a.value_lines or not a.inline_value):
                    second = self._text_pass(image, a.value_lines or self._blind_crop(image, a.line, W, frac=0.12, ext=0.0))
                    if second and resolve_nationality(second[0], self.rules)[0] and second[1] > value_conf:
                        raw, value_conf = second[0], second[1]
                        conf = value_conf * _label_factor(a.score); fv.raw_text = raw; fv.note = "text_pass"
                code, mult, note = resolve_nationality(raw, self.rules)
                fv.normalized = code
                # an exact hit in a closed vocabulary is strong evidence even when OCR confidence is modest
                fv.confidence = round(max(conf, 0.8) if note == "exact" else conf * (mult if code else 0.0), 3)
                fv.note = note
            elif fld == "version_no":
                digits = re.sub(r"\D", "", (raw or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
                fv.normalized = digits or None
            else:
                if fld in self.TEXT_PASS_FIELDS and conf < self.TEXT_PASS_BELOW and image is not None and (a.value_lines or not a.inline_value):
                    second = self._text_pass(image, a.value_lines or self._blind_crop(image, a.line, W, frac=0.22, ext=0.0))
                    if second and second[1] > value_conf + 0.1:
                        raw, value_conf = second[0], second[1]
                        fv.raw_text = raw; fv.confidence = round(value_conf * _label_factor(a.score), 3); fv.note = "text_pass"
                fv.normalized = clean_text(raw)
            res.set(fv)

        # ---- Gregorian date lines (national ID: "DOB: 03/05/1994", "Expiry Date: 26/02/2033") by row overlap ----
        date_anchors = [(a.field, a.line) for a in anchors if a.field in DATE_FIELDS] + [(f, a.line) for f, a in self.secondary.items() if f in DATE_FIELDS]
        ascii_date = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/(?:19|20)\d{2}(?!\d)")
        for l in list(free):
            m = ascii_date.search(l.text or "")
            if not m:
                continue
            best_f, best_ov = None, 0.0
            for fld, al in date_anchors:
                ov = _vertical_overlap(al, l)
                if ov > best_ov:
                    best_f, best_ov = fld, ov
            if best_f and best_ov >= 0.4:
                cur = res.fields.get(best_f)
                d, mult, note = resolve_date(m.group(0))
                if d and (cur is None or cur.normalized is None or cur.confidence < 0.9):
                    conf = round(max(l.confidence, 0.8), 3)
                    if cur is not None and cur.normalized and abs((date.fromisoformat(cur.normalized) - d).days) <= 2:
                        conf = 0.95; note = "gregorian_line xcheck_ok"
                    elif cur is not None and cur.normalized:
                        conf = 0.5   # the two printed copies disagree: one of the reads is wrong -> human decides
                        note = f"gregorian_line xcheck_mismatch({cur.normalized})"
                        res.warnings.append(f"{best_f}: Hijri/Gregorian copies disagree ({cur.normalized} vs {d.isoformat()})")
                    res.set(FieldValue(field=best_f, raw_text=m.group(0), normalized=d.isoformat(), confidence=conf, bbox=l.bbox, source="pattern", note=note))
                    free.remove(l)

        # ---- pattern fallbacks for the two IDs ----
        if res.value("iqama_no") is None or res.value("employer_id") is None:
            found: list[tuple[str, float, OCRLine]] = []
            for l in lines:
                if ascii_date.search(l.text or "") or re.search(r"[0-9٠-٩]{4}/[0-9٠-٩]{1,2}/[0-9٠-٩]{1,2}", l.text or ""):
                    continue  # never mine an ID out of a date line
                for part in (l.parts or [l]):
                    n, mult, _ = resolve_ten_digit(part.text, "127")
                    if n and len(n) == 10:
                        found.append((n, part.confidence * mult * (1.0 if luhn_ok(n) else 0.7), part))
            if res.value("iqama_no") is None:
                c = [f for f in found if f[0][0] == ("1" if is_national else "2")]
                if c:
                    n, cf, l = max(c, key=lambda t: t[1])
                    res.set(FieldValue(field="iqama_no", raw_text=l.text, normalized=n, confidence=round(max(cf * 0.95, 0.8 if luhn_ok(n) else 0.0), 3), bbox=l.bbox, source="pattern", note="pattern_fallback"))
                    res.warnings.append("iqama_no found by pattern, not by label")
            if res.value("employer_id") is None and self._preliminary_layout(lines) != "old":
                iq = res.value("iqama_no") or ""
                c = [f for f in found if f[0][0] in "127" and luhn_ok(f[0])
                     and re.search(r"(?<!\d)\d{10}(?!\d)", f[2].text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))   # one contiguous run
                     and sum(a != b for a, b in zip(f[0], iq)) > 2]                                                        # not the Iqama misread
                if c:
                    n, cf, l = max(c, key=lambda t: t[1])
                    res.set(FieldValue(field="employer_id", raw_text=l.text, normalized=n, confidence=round(max(cf * 0.95, 0.78 if luhn_ok(n) else 0.0), 3), bbox=l.bbox, source="pattern", note="pattern_fallback"))
                    res.warnings.append("employer_id found by pattern, not by label")

        self._infer_by_row_order(res, anchors, free, W)
        self._infer_nationality(res, anchors, free, W)
        fa, fe = self._names(anchors, free, W, H)
        if fa: res.set(fa)
        if fe: res.set(fe)
        res.doc_type = self._detect_doc_type(res, lines)
        res.layout = self._detect_layout(res, anchors, lines) if res.doc_type == "IQAMA" else "unknown"
        return res

    def _doc_type_cues(self, lines: list[OCRLine]) -> int:
        texts = " ".join(normalize_arabic(l.text) or "" for l in lines)
        low = " ".join((l.text or "").lower() for l in lines)
        cues = sum(1 for k in ("الهويه الوطنيه", "بطاقه الهويه") if k in texts)
        cues += sum(1 for k in ("id number", "date of birth", "expiry date", "dob:", "doe:", "dob", "doe") if k in low)
        return cues

    def _detect_doc_type(self, res: ExtractionResult, lines: list[OCRLine]) -> str:
        """Saudi national ID ("الهوية الوطنية", English labels, number prefix 1) vs Iqama (prefix 2)."""
        texts = " ".join(normalize_arabic(l.text) or "" for l in lines)
        cues = self._doc_type_cues(lines)
        iq = res.value("iqama_no") or ""
        if cues >= 1 or (iq.startswith("1") and "هويه مقيم" not in texts):
            # user rule: a national ID holder is always Saudi, whatever else was read on the card
            res.set(FieldValue(field="nationality", raw_text=None, normalized=self.rules.config.national_id.nationality_code,
                               confidence=1.0, source="derived", note="national_id"))
            return "NATIONAL_ID"
        return "IQAMA"

    # ---------- layout ----------
    _OLD_HEADER = ("kingdom of saudi arabia", "ministry of interior", "resident identity")

    def _old_header_score(self, lines: list[OCRLine]) -> int:
        """The old green card carries an English header; the current one is Arabic-only. OCR garbles it, so fuzzy."""
        hits = 0
        for l in lines:
            if has_latin(l.text) and self.H and l.y1 < 0.25 * self.H:
                t = (l.text or "").lower()
                if any(fuzz.partial_ratio(h, t) >= 80 for h in self._OLD_HEADER):
                    hits += 1
        return hits

    def _preliminary_layout(self, lines: list[OCRLine]) -> str:
        return "old" if self._old_header_score(lines) >= 2 else "unknown"

    def _detect_layout(self, res: ExtractionResult, anchors: list[Anchor], lines: list[OCRLine]) -> str:
        """Old green 'Resident Identity' layout vs the current 'هوية مقيم' layout, from independent cues."""
        old = 0; new = 0
        texts = " ".join(normalize_arabic(l.text) or "" for l in lines)
        old += min(3, self._old_header_score(lines))
        if (res.fields.get("expiry_date") or FieldValue(field="x")).note and "hijri" in (res.fields["expiry_date"].note or ""):
            old += 2
        bare = {"الرقم", "الانتهاء", "الميلاد", "صاحب العمل", "الاصدار"}
        for a in anchors:
            m = self._best_label(normalize_arabic(a.line.text) or "", anchored_start=True, relaxed=True)
            # which variant matched? approximate: a short label text (<= 9 chars) for these fields signals the old layout
            if a.field in ("iqama_no", "expiry_date", "birth_date", "employer_name", "issue_place") and len((normalize_arabic(a.line.text) or "").split(":")[0].strip()) <= 9:
                old += 1
        if res.value("employer_id"):
            new += 2
        if any(a.field == "employer_id" for a in anchors):
            new += 1
        if "هويه مقيم" in texts and "رقم النسخه" in texts:
            new += 1
        if old >= 3 and old > new:
            return "old"
        if new >= 2 and new >= old:
            return "new"
        return "unknown"

    # ---------- nationality by vocabulary ----------
    def _infer_nationality(self, res: ExtractionResult, anchors: list[Anchor], free: list[OCRLine], W: int) -> None:
        """Label garbled/missing: the nationality value is a country name from a closed list, printed in the
        right-hand value column (x >= 0.7 W). Birth place also holds a country but sits in the middle column."""
        if res.value("nationality"):
            return
        best = None
        for l in free:
            if not has_arabic(l.text) or l.x1 < 0.7 * W:
                continue
            code, mult, note = resolve_nationality(clean_text(l.text), self.rules)
            if code and mult >= 0.85 and (best is None or mult > best[1]):
                best = (l, mult, code, note)
        if best:
            l, mult, code, note = best
            res.set(FieldValue(field="nationality", raw_text=clean_text(l.text), normalized=code, confidence=round(max(l.confidence * mult, 0.8 if note == "exact" else 0.0), 3),
                               bbox=l.bbox, source="ocr", note=f"vocab_inference:{note}"))
            free.remove(l)
            res.warnings.append("nationality: label unreadable; value matched from country list")

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
