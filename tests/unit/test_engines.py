from datetime import date, timedelta

import pytest

from app.core.clock import FixedClock
from app.engines.decision import decide
from app.engines.employer import classify_employer, check_employer
from app.engines.expiry import check_expiry
from app.engines.nationality import check_nationality
from app.engines.occupation import check_occupation, match_occupation


S1 = dict(iqama_no="2401246992", expiry_date="2022-12-11", nationality="SD", occupation="سائق خاص", employer_id="1052885942", employer_name="محمد سعد فهد القحطاني")
S2 = dict(iqama_no="2627946219", expiry_date="2026-12-30", nationality="SD", occupation="عامل تحميل وتنزيل", employer_id="7034514229", employer_name="مؤسسة عبيد محمد البيشي")
S3 = dict(iqama_no="2513399374", expiry_date="2026-04-02", nationality="SD", occupation="موصل كابلات كهربائية", employer_id="7015944221", employer_name="شركة ركائز جده للمقاولات المعمارية")


def test_expiry_states(rules, make_x, clock):
    assert check_expiry(make_x(expiry_date="2022-12-11"), rules, clock).label == "EXPIRED"
    assert check_expiry(make_x(expiry_date="2026-12-30"), rules, clock).label == "VALID"
    soon = (clock.today() + timedelta(days=10)).isoformat()
    r = check_expiry(make_x(expiry_date=soon), rules, clock)
    assert r.label == "EXPIRING_SOON" and r.outcome == "REVIEW" and r.details["days_remaining"] == 10
    assert check_expiry(make_x(), rules, clock).label == "DATE_NOT_READABLE"
    # never a constant date: a later clock flips VALID -> EXPIRED
    assert check_expiry(make_x(expiry_date="2026-12-30"), rules, FixedClock(date(2027, 1, 1))).label == "EXPIRED"


def test_employer_prefix_rule_dominates(rules):
    assert classify_employer("1052885942", "محمد سعد فهد القحطاني", rules)[0] == "INDIVIDUAL"
    assert classify_employer("7034514229", "مؤسسة عبيد محمد البيشي", rules)[0] == "COMPANY"
    assert classify_employer("7015944221", "شركة ركائز جده للمقاولات المعمارية", rules)[0] == "COMPANY"
    assert classify_employer("2627946219", "أحمد علي", rules)[0] == "INDIVIDUAL"
    # name alone never reaches the auto-reject threshold
    t, conf, _ = classify_employer(None, "محمد سعد فهد القحطاني", rules)
    assert t == "INDIVIDUAL" and conf < rules.config.employer.individual_auto_reject_threshold
    # contradiction between prefix and name lowers confidence below auto-reject
    t, conf, ev = classify_employer("1052885942", "شركة النور للمقاولات", rules)
    assert t == "INDIVIDUAL" and conf < 0.85 and any(e["signal"] == "name_contradicts_prefix" for e in ev)
    assert classify_employer(None, "وزارة الصحة", rules)[0] == "GOVERNMENT"
    assert classify_employer(None, None, rules)[0] == "UNKNOWN"


def test_employer_check_outcomes(rules, make_x):
    assert check_employer(make_x(employer_id="1052885942", employer_name="محمد سعد فهد القحطاني"), rules).outcome == "FAIL"
    assert check_employer(make_x(employer_name="محمد سعد فهد القحطاني"), rules).outcome == "REVIEW"
    assert check_employer(make_x(employer_id="7034514229"), rules).outcome == "PASS"
    assert check_employer(make_x(), rules).label == "UNKNOWN"


def test_occupation_matching(rules, make_x):
    assert match_occupation("سائق خاص", rules)[2] == "exact"
    assert match_occupation("سائق منزل", rules)[2] == "alias"
    assert match_occupation("طبّاخ", rules)[0].occupation_en == "Cook"
    row, conf, method = match_occupation("سائق  خاص.", rules)
    assert row is not None
    assert check_occupation(make_x(occupation="سائق خاص"), rules).outcome == "FAIL"
    r = check_occupation(make_x(occupation="عامل تحميل وتنزيل"), rules)
    assert r.outcome == "REVIEW" and r.label == "UNKNOWN"          # ALLOWLIST: unlisted -> review


def test_nationality_modes(rules, make_x):
    assert check_nationality(make_x(nationality="SD"), rules).outcome == "PASS"
    assert check_nationality(make_x(), rules).outcome == "REVIEW"
    # BLOCKLIST mode via a modified snapshot
    from app.engines.rules import RulesSnapshot
    files = dict(rules.files)
    files["rules.yaml"] = files["rules.yaml"].replace("mode: ALL_APPROVED", "mode: BLOCKLIST")
    files["nationalities.csv"] = files["nationalities.csv"].replace("SD,السودان,Sudan,سوداني|سودانية|السودانية,Yes", "SD,السودان,Sudan,سوداني|سودانية|السودانية,No")
    snap = RulesSnapshot(files)
    r = check_nationality(make_x(nationality="SD"), snap)
    assert r.outcome == "FAIL" and "not approved" in r.reason


def test_decision_matrix_on_samples(rules, make_x, clock):
    d1 = decide(make_x(**S1), rules, clock)
    assert d1.status == "REJECTED" and len(d1.reasons) == 3
    assert d1.reasons[0].startswith("Iqama Expired") and d1.reasons[1] == "Individual Employer" and "Private Driver" in d1.reasons[2]
    d2 = decide(make_x(**S2), rules, clock)
    assert d2.status == "MANUAL_REVIEW" and d2.recommendation == "NEEDS_ATTENTION"
    d3 = decide(make_x(**S3), rules, clock)
    assert d3.status == "REJECTED" and d3.reasons == ["Iqama Expired (2026-04-02, -155 days)"]


def test_decision_gates(rules, make_x, clock):
    # all pass, auto_approve=false -> review with RECOMMEND_APPROVE
    x = make_x(**{**S2, "occupation": "سائق خاص"})
    from app.engines.rules import RulesSnapshot
    files = dict(rules.files)
    files["occupations.csv"] += ",نجار,Carpenter,Construction,Yes,,,admin,2026-09-04\n"
    snap = RulesSnapshot(files)
    x = make_x(**{**S2, "occupation": "نجار"})
    d = decide(x, snap, clock)
    assert d.status == "MANUAL_REVIEW" and d.recommendation == "RECOMMEND_APPROVE"
    files["rules.yaml"] = files["rules.yaml"].replace("auto_approve: false", "auto_approve: true")
    assert decide(x, RulesSnapshot(files), clock).status == "APPROVED"
    # poor image dominates even a hard fail
    assert decide(make_x(quality=0.2, **S1), rules, clock).status == "MANUAL_REVIEW"
    # low-confidence hard fail -> review, not reject
    d = decide(make_x(conf=0.5, **S1), rules, clock)
    assert d.status == "MANUAL_REVIEW" and any("low confidence" in t for t in d.review_triggers)
    # expiring soon -> review with days remaining
    soon = (clock.today() + timedelta(days=5)).isoformat()
    d = decide(make_x(**{**S2, "expiry_date": soon}), rules, clock)
    assert d.status == "MANUAL_REVIEW" and any("5 days remaining" in t for t in d.review_triggers)
    # human confirmation on reject
    files2 = dict(rules.files)
    files2["rules.yaml"] = files2["rules.yaml"].replace("require_human_confirmation_on_reject: false", "require_human_confirmation_on_reject: true")
    d = decide(make_x(**S1), RulesSnapshot(files2), clock)
    assert d.status == "MANUAL_REVIEW" and d.recommendation == "RECOMMEND_REJECT" and len(d.reasons) == 3


def test_rules_validation_rejects_bad_file(rules):
    from app.engines.rules import RulesLoadError, RulesSnapshot
    files = dict(rules.files)
    files["rules.yaml"] = files["rules.yaml"].replace("warn_days: 30", "warn_days: -5")
    with pytest.raises(RulesLoadError):
        RulesSnapshot(files)
    files = dict(rules.files)
    files["occupations.csv"] = "code,occupation_ar\n,x\n"   # missing eligible column
    with pytest.raises(RulesLoadError):
        RulesSnapshot(files)
