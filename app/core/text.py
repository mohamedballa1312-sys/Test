"""Arabic / digit normalization shared by pipeline and engines (no business rules here)."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_EASTERN_ARABIC_INDIC = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_WS = re.compile(r"\s+")
# common OCR confusions inside numeric fields only
_DIGIT_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8", "Z": "2"})
_DATE_RE = re.compile(r"(\d{4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})")
_DATE_RE_DMY = re.compile(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4})")


def normalize_digits(s: str | None) -> str | None:
    if s is None:
        return None
    return s.translate(_ARABIC_INDIC).translate(_EASTERN_ARABIC_INDIC)


def normalize_numeric_field(s: str | None) -> str | None:
    """Digits only, after Arabic-Indic conversion and OCR-confusion repair."""
    if s is None:
        return None
    s = normalize_digits(s).translate(_DIGIT_CONFUSIONS)
    return re.sub(r"\D", "", s)


def normalize_arabic(s: str | None) -> str | None:
    """Canonical form for matching: no diacritics/tatweel, unified alef/teh-marbuta/yeh, single spaces, lowercase latin."""
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", s)
    s = _TASHKEEL.sub("", s).replace(_TATWEEL, "")
    s = re.sub("[إأآٱ]", "ا", s)
    s = s.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    s = s.replace("،", ",").replace("؛", ";").replace("：", ":")
    s = _WS.sub(" ", s).strip().lower()
    return s


def has_arabic(s: str) -> bool:
    return bool(re.search(r"[؀-ۿ]", s or ""))


def has_latin(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s or ""))


HIJRI_MIN, HIJRI_MAX = 1300, 1499


def is_hijri_year(y: int) -> bool:
    return HIJRI_MIN <= y <= HIJRI_MAX


def hijri_to_gregorian(y: int, m: int, d: int) -> date | None:
    """Umm al-Qura conversion (older Iqama layouts print the expiry in Hijri)."""
    try:
        from hijridate import Hijri
        g = Hijri(y, m, d).to_gregorian()
        return date(g.year, g.month, g.day)
    except Exception:
        return None


def parse_date(s: str | None) -> date | None:
    """Card prints YYYY/MM/DD in Arabic-Indic digits — Gregorian on current cards, Hijri on older ones.
    Hijri years (1300-1499) are converted to Gregorian. Also accepts DD/MM/YYYY."""
    if not s:
        return None
    t = normalize_digits(s)
    m = _DATE_RE.search(t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = _DATE_RE_DMY.search(t)
        if not m:
            return None
        d, mo, y = (int(x) for x in m.groups())
    if is_hijri_year(y):
        return hijri_to_gregorian(y, mo, d)
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def find_dates(s: str) -> list[date]:
    t = normalize_digits(s or "")
    out: list[date] = []
    for m in _DATE_RE.finditer(t):
        y, mo, d = (int(x) for x in m.groups())
        try:
            out.append(date(y, mo, d))
        except ValueError:
            pass
    return out


def find_ten_digit_numbers(s: str) -> list[str]:
    t = normalize_digits(s or "")
    return re.findall(r"(?<!\d)(\d{10})(?!\d)", t)


def luhn_ok(number: str) -> bool:
    """Luhn check as commonly applied to Saudi 10-digit IDs. Advisory signal only (see Phase 1 FR-VAL-02)."""
    if not number or not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(number):
        d = int(ch)
        if i % 2 == 0:  # positions 1,3,5,.. (1-based odd) doubled
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
