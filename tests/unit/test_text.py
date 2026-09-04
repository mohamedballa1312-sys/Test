from datetime import date

from app.core.text import luhn_ok, normalize_arabic, normalize_digits, parse_date
from app.pipeline.normalize import clean_text, resolve_date, resolve_ten_digit


def test_digits_and_arabic_normalization():
    assert normalize_digits("٢٠٢٦/١٢/٣٠") == "2026/12/30"
    assert normalize_arabic("مؤسّسة  الخليجِ ") == "موسسه الخليج"
    assert normalize_arabic("سائق خاص") == normalize_arabic("سايق خاص")


def test_parse_date_formats():
    assert parse_date("٢٠٢٦/١٢/٣٠") == date(2026, 12, 30)
    assert parse_date("30/12/2026") == date(2026, 12, 30)
    assert parse_date("garbage") is None


def test_luhn_on_real_prefixes():
    # both Iqama (2...) and unified establishment numbers (7...) carry a Luhn checksum
    assert luhn_ok("2401246992") and luhn_ok("7015944221") and luhn_ok("1052885942")
    assert not luhn_ok("2401246993")


def test_ten_digit_chunk_order_resolution():
    assert resolve_ten_digit("٢٤٠ ١٢٤٦٩٩٢", "2")[0] == "2401246992"      # natural order valid
    assert resolve_ten_digit("٩ ٢٦٢٧٩٤٦٢١", "2")[0] == "2627946219"      # reversed chunks
    n, mult, _ = resolve_ten_digit("١٥٩٤٤٢٢١ ٧٠", "127")                # ambiguous -> reversed preferred, low mult
    assert n == "7015944221" and mult < 0.85
    assert resolve_ten_digit("5اا5 ٧٠١٥٩٤٤٢٢١", "127") == ("7015944221", 1.0, "exact_chunk(luhn=True)")


def test_date_resolution_variants():
    assert resolve_date("٣٠ ١٢ /٢٦ ٢٠")[0] == date(2026, 12, 30)   # reversed + dropped separator
    assert resolve_date("٢٠٢٦/١٢٨٣٠")[0] == date(2026, 12, 30)     # '/' misread as 8
    assert resolve_date("٢ ٠٢٢/١٢١١")[0] == date(2022, 12, 11)     # 8 digits across chunks
    assert resolve_date("٢٦/١٢/٣٠")[0] == date(2026, 12, 30)       # YY/MM/DD
    assert resolve_date("٢٦/٠٤١ ٢٠")[0] is None                    # damaged beyond repair


def test_clean_text_strips_stray_letters():
    assert clean_text("ز موسسه عبيد") == "موسسه عبيد"
    assert clean_text(": السودان :") == "السودان"
