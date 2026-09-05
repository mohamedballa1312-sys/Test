from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectInfo(BaseModel):
    name: str = ""
    location: str = ""
    work_start: str = ""
    work_end_expected: str = ""


class BatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    requesting_companies: list[str] = Field(default_factory=list)   # empty -> rules.permit.default_requesting_companies
    project: ProjectInfo = Field(default_factory=ProjectInfo)


class BatchProjectUpdate(BaseModel):
    requesting_companies: list[str] | None = None
    project: ProjectInfo | None = None


class CompanyChoice(BaseModel):
    company_name: str = Field(min_length=1, max_length=300)


class BatchOut(BaseModel):
    id: int
    name: str
    status: str
    total: int
    processed: int
    created_at: str
    rules_version: str | None
    requesting_companies: list[str] = Field(default_factory=list)
    project: dict[str, str] = Field(default_factory=dict)
    permit_exported_at: str | None = None
    summary: dict[str, int] | None = None


class UploadOut(BaseModel):
    accepted: list[int]
    rejected: list[dict[str, str]]


class FieldOut(BaseModel):
    field: str
    raw_text: str | None
    normalized: str | None
    confidence: float
    bbox: list[int] | None
    source: str
    note: str | None
    corrected_by: str | None


class DocumentOut(BaseModel):
    id: int
    batch_id: int
    filename: str
    page_no: int
    status: str
    error: str | None
    quality_score: float | None
    has_image: bool
    duplicate_of: int | None
    doc_type: str | None = None
    company_final: str | None = None
    company_source: str | None = None
    company_options: list[str] = Field(default_factory=list)
    fields: dict[str, FieldOut]
    decision: dict[str, Any] | None
    warnings: list[str]


class Corrections(BaseModel):
    fields: dict[str, str | None]


class ReviewSubmit(BaseModel):
    status: Literal["APPROVED", "REJECTED"]
    note: str | None = None


class OccupationUpsert(BaseModel):
    occupation_ar: str
    occupation_en: str = ""
    category: str = ""
    eligible: bool
    reason: str = ""
    aliases: list[str] = Field(default_factory=list)
    code: str = ""


class RulesFile(BaseModel):
    content: str
