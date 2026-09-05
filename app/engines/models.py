"""Shared data contracts between pipeline, engines, services (pydantic for JSON round-trips)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FieldSource = Literal["ocr", "manual", "derived", "pattern"]
Outcome = Literal["PASS", "FAIL", "REVIEW", "UNKNOWN"]
Status = Literal["APPROVED", "REJECTED", "MANUAL_REVIEW"]
CheckName = Literal["NATIONALITY", "EXPIRY", "EMPLOYER", "OCCUPATION"]

FIELD_NAMES = [
    "iqama_no", "name_ar", "name_en", "expiry_date", "birth_date", "birth_place",
    "nationality", "religion", "occupation", "employer_id", "employer_name",
    "issue_place", "work_place", "version_no",
]


class FieldValue(BaseModel):
    field: str
    raw_text: str | None = None
    normalized: str | None = None
    confidence: float = 0.0
    bbox: tuple[int, int, int, int] | None = None  # x, y, w, h on the processed card image
    source: FieldSource = "ocr"
    corrected_by: str | None = None
    corrected_at: datetime | None = None
    note: str | None = None


class ExtractionResult(BaseModel):
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    quality_score: float = 0.0
    ocr_provider: str = ""
    card_size: tuple[int, int] | None = None  # w, h after preprocessing
    raw_lines: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    anchors_found: int = 0
    layout: Literal["new", "old", "unknown"] = "unknown"   # "old" = green "Resident Identity" card (Hijri expiry, no employer ID)

    def value(self, name: str) -> str | None:
        f = self.fields.get(name)
        return f.normalized if f else None

    def raw(self, name: str) -> str | None:
        f = self.fields.get(name)
        return f.raw_text if f else None

    def confidence(self, name: str) -> float:
        f = self.fields.get(name)
        return f.confidence if f else 0.0

    def date_value(self, name: str) -> date | None:
        v = self.value(name)
        return date.fromisoformat(v) if v else None

    def set(self, fv: FieldValue) -> None:
        self.fields[fv.field] = fv


class CheckResult(BaseModel):
    check: CheckName
    outcome: Outcome
    label: str
    confidence: float = 0.0
    reason: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    rules_version: str = ""


class Decision(BaseModel):
    status: Status
    reasons: list[str] = Field(default_factory=list)
    review_triggers: list[str] = Field(default_factory=list)
    recommendation: Literal["RECOMMEND_APPROVE", "RECOMMEND_REJECT", "NEEDS_ATTENTION"] | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    rules_version: str = ""
    decided_at: datetime
    decided_by: str = "system"
    version: int = 1
    quality_score: float = 0.0
