"""Company name shown on the permit form (user rule, 2026-09-05).

Iqama: the card's employer is the primary name; if it matches one of the requesting companies it is adopted
directly, otherwise the reviewer chooses between the card's employer and a requesting company.
National ID: the requesting company, directly.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from app.core.text import normalize_arabic
from app.engines.rules import RulesSnapshot


def resolve_company(card_employer: str | None, requesting: list[str], doc_type: str, rules: RulesSnapshot) -> tuple[str | None, str, bool]:
    """Returns (company_name, source, needs_choice)."""
    requesting = [r.strip() for r in requesting if r and r.strip()] or list(rules.config.permit.default_requesting_companies)
    if doc_type == "NATIONAL_ID":
        return (requesting[0] if requesting else None), "NATIONAL_ID", False
    emp = normalize_arabic(card_employer) or ""
    if emp:
        best = None
        for r in requesting:
            score = max(fuzz.token_set_ratio(emp, normalize_arabic(r)), fuzz.partial_ratio(emp, normalize_arabic(r))) / 100.0
            if score >= rules.config.permit.company_match_threshold and (best is None or score > best[1]):
                best = (r, score)
        if best:
            return best[0], "AUTO", False
        return card_employer, "CARD", True
    return (requesting[0] if requesting else None), "CARD", True
