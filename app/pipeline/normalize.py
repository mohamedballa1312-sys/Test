"""Field-level normalization incl. resolving RTL chunk-order ambiguity in OCR'd numbers and dates."""
from __future__ import annotations

import re
from datetime import date

from app.core.text import luhn_ok, normalize_arabic, normalize_digits, normalize_numeric_field, parse_date
from app.engines.rules import RulesSnapshot

_CHUNK = re.compile(r"[0-9]+")


def _digit_chunks(text: str) -> list[str]:
    return _CHUNK.findall(normalize_numeric_field_keep_sep(text))


def normalize_numeric_field_keep_sep(text: str) -> str:
    t = normalize_digits(text or "")
    t = t.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"}))
    return re.sub(r"[^0-9/ \-.]", " ", t)


def resolve_ten_digit(text: str | None, allowed_prefixes: str) -> tuple[str | None, float, str]:
    """OCR may emit multi-chunk Arabic-Indic numbers in visual (reversed) chunk order.
    Try natural and reversed joins; prefer the candidate with a valid prefix, then Luhn.
    Returns (number, confidence_multiplier, note)."""
    if not text:
        return None, 0.0, "empty"
    chunks = _digit_chunks(text)
    if not chunks:
        return None, 0.0, "no_digits"
    # a single clean 10-digit chunk with a valid prefix + checksum beats anything around it (label residue, noise)
    exact = [c for c in chunks if len(c) == 10 and c[0] in allowed_prefixes and luhn_ok(c)]
    if len(exact) == 1:
        return exact[0], 1.0, "exact_chunk(luhn=True)"
    joined = "".join(chunks)
    if len(joined) != 10:
        # single-chunk repair: nothing to reorder
        return (joined if joined else None), 0.5, f"length_{len(joined)}"
    candidates = [("".join(chunks), "natural"), ("".join(reversed(chunks)), "reversed")]
    scored = []
    for cand, how in candidates:
        score = 0
        if cand[0] in allowed_prefixes:
            score += 2
        if luhn_ok(cand):
            score += 1
        scored.append((score, cand, how))
    # tie-break toward "reversed": Arabic OCR models emit multi-chunk numbers in visual order most of the time
    scored.sort(key=lambda t: (-t[0], 0 if t[2] == "reversed" else 1))
    best_score, best, how = scored[0]
    if len(chunks) == 1:
        mult = 1.0 if best_score >= 2 else 0.7
        return best, mult, f"single_chunk(prefix_ok={best[0] in allowed_prefixes},luhn={luhn_ok(best)})"
    if best_score == 0:
        return best, 0.5, "no_candidate_valid"
    # ambiguity: both orders valid prefix -> lower confidence
    if len(scored) > 1 and scored[1][0] >= 2:
        return best, 0.75, f"ambiguous_order({how})"
    return best, 1.0 if best_score >= 3 else 0.9, f"{how}(luhn={luhn_ok(best)})"


def resolve_date(text: str | None) -> tuple[date | None, float, str]:
    """Card format YYYY/MM/DD. Handles dropped separators and reversed chunk order."""
    if not text:
        return None, 0.0, "empty"
    d = parse_date(text)
    if d and 1900 <= d.year <= 2100:
        return d, 1.0, "direct"
    chunks = _digit_chunks(text)
    if not chunks:
        return None, 0.0, "no_digits"
    for how, seq in (("natural", chunks), ("reversed", list(reversed(chunks)))):
        s = "".join(seq)
        # a '/' misread as a digit (typically ٨/8, also 1/7) inside YYYY/MM/DD or YY/MM/DD
        rep = _repair_separator_misread(s)
        if rep:
            return rep, 0.7, f"sep_repair_{how}"
        if len(s) == 8:
            try:
                dd = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                if 1900 <= dd.year <= 2100:
                    return dd, 0.85 if how == "natural" else 0.8, f"8digits_{how}"
            except ValueError:
                pass
        # YY/MM/DD (leading century digits lost)
        if len(s) == 6 and len(seq) == 3:
            try:
                dd = date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))
                return dd, 0.6, f"yymmdd_{how}"
            except ValueError:
                pass
        # try re-inserting separators between chunks
        for cand in ("/".join(seq),):
            dd = parse_date(cand)
            if dd and 1900 <= dd.year <= 2100:
                return dd, 0.6, f"rejoined_{how}"
    return None, 0.0, "unparseable"


_SEP_LOOKALIKES = set("8")  # only ٨/8 observed as a misread slash; 1/7 caused false dates


def _repair_separator_misread(s: str) -> date | None:
    """'20261 2830' -> digits '202612830' (9): drop a separator-lookalike at a separator position."""
    def _try(y: str, m: str, d: str) -> date | None:
        try:
            dd = date(int(y), int(m), int(d))
            return dd if 1900 <= dd.year <= 2100 else None
        except ValueError:
            return None
    n = len(s)
    if n == 9:                       # YYYYMM?DD or YYYY?MMDD
        if s[6] in _SEP_LOOKALIKES and (r := _try(s[:4], s[4:6], s[7:9])):
            return r
        if s[4] in _SEP_LOOKALIKES and (r := _try(s[:4], s[5:7], s[7:9])):
            return r
    if n == 10 and s[4] in _SEP_LOOKALIKES and s[7] in _SEP_LOOKALIKES:   # YYYY?MM?DD
        return _try(s[:4], s[5:7], s[8:10])
    return None


def resolve_nationality(text: str | None, rules: RulesSnapshot) -> tuple[str | None, float, str]:
    """Map printed nationality (country name) to ISO code via the nationalities table; fuzzy if needed."""
    if not text:
        return None, 0.0, "empty"
    row = rules.nationality_lookup(text)
    if row:
        return row.code, 1.0, "exact"
    from rapidfuzz import fuzz, process
    keys = rules.nationality_keys()
    best = process.extractOne(normalize_arabic(text), [k for k, _ in keys], scorer=fuzz.ratio)
    if best and best[1] >= 80:
        return keys[best[2]][1].code, round(best[1] / 100.0, 2), "fuzzy"
    return None, 0.0, "unmatched"


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    t = re.sub(r"^[\s:：;؛.,'\"_\-]+|[\s:：;؛.,'\"_\-]+$", "", text)
    t = re.sub(r"\s+", " ", t)
    # OCR turns separators into stray single letters ("ز", "ه", "ء") at the edges of a value
    t = re.sub(r"^(?:[\u0621-\u064A]\s+)+", "", t)
    t = re.sub(r"(?:\s+[\u0621-\u064A])+$", "", t)
    return t or None
