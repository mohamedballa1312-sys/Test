"""Extractor on synthetic OCR lines mimicking the real card layout (fake identities)."""
from app.engines.rules import RulesSnapshot
from app.pipeline.extract import Extractor, merge_rows
from app.pipeline.ocr.base import OCRLine

W, H = 1600, 1000


def L(text, x1, x2, y1, y2, conf=0.9):
    return OCRLine(text, (x1, y1, x2 - x1, y2 - y1), conf)


def card_lines(with_work_place=True):
    lines = [
        L("هوية مقيم", 375, 554, 44, 135), L("رقم النسخة", 244, 547, 146, 209), L("وزارة الداخلية", 1040, 1341, 168, 235),
        L("خالد سعيد عمر حسن", 928, 1544, 267, 346), L("KHALID SAEED OMAR HASSAN", 736, 1535, 344, 406),
        L("رقم الهوية :", 1377, 1535, 459, 499), L("٢١٢٣٤٥٦٧٨٤", 1160, 1353, 449, 489),
        L("تاريخ الانتهاء:", 811, 993, 449, 491), L("٢٠٢٧/٠٣/١٥", 604, 720, 448, 478),
        L("تاريخ الميلاد", 1360, 1537, 516, 564), L("١٩٩٠/٠١/٠١", 1167, 1333, 515, 555),
        L("مكان الميلاد:", 803, 989, 515, 557), L("السودان", 669, 791, 515, 555),
        L("الجنسية :", 1411, 1537, 585, 625), L("السودان", 1231, 1355, 585, 623),
        L("الديانة", 895, 991, 577, 613), L("الاسلام", 677, 791, 575, 617),
        L("المهنة :", 1430, 1530, 652, 682), L("نجار", 1200, 1352, 643, 687),
        L("هوية صاحب العمل: ٧٠٠١٢٣٤٥٦٣", 1060, 1531, 706, 758),
        L("مكان الإصدار:   موقع بوابة الوزارة الألكترونية", 920, 1529, 766, 822),
    ]
    y = 835
    if with_work_place:
        lines += [L("مكان", 1442, 1526, y, y + 49), L("العمل", 1353, 1447, y + 6, y + 46), L("منطقة", 1228, 1339, y + 1, y + 40), L("عسير", 1139, 1233, y + 5, y + 45)]
        y = 897
    lines += [L("اسم صاحب العمل :", 1260, 1531, y + 7, y + 48), L("شركة الأمل للمقاولات", 900, 1250, y + 8, y + 46),
              L("يجب التحقق", 349, 483, 771, 809), L("من الرمز السريع", 299, 483, 808, 845)]
    return lines


def test_extracts_all_fields_with_and_without_work_place(rules):
    for wp in (True, False):
        x = Extractor(rules, None).extract(card_lines(wp), None, W, H)
        assert x.value("iqama_no") == "2123456784"
        assert x.value("expiry_date") == "2027-03-15"
        assert x.value("birth_date") == "1990-01-01"
        assert x.value("nationality") == "SD"
        assert x.value("occupation") == "نجار"
        assert x.value("employer_id") == "7001234563"
        assert "الامل" in x.value("employer_name") or "الأمل" in x.value("employer_name")
        assert x.value("name_ar") == "خالد سعيد عمر حسن"
        assert x.value("name_en") == "KHALID SAEED OMAR HASSAN"
        assert (x.value("work_place") == "منطقه عسير") == wp
        assert x.fields["iqama_no"].confidence >= 0.75


def test_garbled_label_recovered_by_row_order(rules):
    lines = [l for l in card_lines() if not l.text.startswith("المهنة")]
    lines.append(L("اشهنة :", 1430, 1530, 652, 682, 0.12))          # garbled "المهنة:"
    x = Extractor(rules, None).extract(lines, None, W, H)
    assert x.value("occupation") == "نجار" and x.fields["occupation"].note == "row_order_inference"


def test_qr_caption_never_becomes_anchor(rules):
    x = Extractor(rules, None).extract(card_lines(), None, W, H)
    assert x.value("work_place") != "بالسربم"
    assert all(v.bbox[0] >= 0.3 * W for k, v in x.fields.items() if v.bbox)


def test_merge_rows_keeps_parts():
    a = L("هوية", 1435, 1526, 750, 808); b = L("صاحب العمل:", 1222, 1443, 751, 804)
    m = merge_rows([a, b])
    assert len(m) == 1 and m[0].parts and m[0].text == "هوية صاحب العمل:"
