"""RulesRepository: loads, validates and versions every file under config/. Read-only for engines."""
from __future__ import annotations

import csv
import hashlib
import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.text import normalize_arabic


# ---------- rules.yaml schema ----------
class ImageRules(BaseModel):
    min_quality_score: float = 0.45
    min_card_width_px: int = 600
    upscale_below_px: int = 1200


class LayoutRules(BaseModel):
    # old green "Resident Identity" card: request the current Absher printout instead of deciding on it
    old_layout_action: Literal["REVIEW", "IGNORE"] = "REVIEW"
    old_layout_note: str = "OLD CARD LAYOUT - please provide the Absher copy / برجاء توفير نسخة أبشر"


class OCRExternal(BaseModel):
    enabled: bool = False
    acknowledged_by: str | None = None


class OCRRules(BaseModel):
    provider: str = "paddle"
    min_field_confidence: float = 0.75
    critical_fields: list[str] = Field(default_factory=lambda: ["iqama_no", "expiry_date", "nationality", "employer_id", "occupation"])
    external: OCRExternal = Field(default_factory=OCRExternal)


class ChecksRules(BaseModel):
    order: list[Literal["NATIONALITY", "EXPIRY", "EMPLOYER", "OCCUPATION"]] = Field(
        default_factory=lambda: ["NATIONALITY", "EXPIRY", "EMPLOYER", "OCCUPATION"]
    )


class NationalityRules(BaseModel):
    mode: Literal["ALL_APPROVED", "BLOCKLIST", "ALLOWLIST"] = "ALL_APPROVED"
    file: str = "nationalities.csv"


class ExpiryRules(BaseModel):
    timezone: str = "Asia/Riyadh"
    warn_days: int = 30

    @field_validator("warn_days")
    @classmethod
    def _nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError("warn_days must be >= 0")
        return v


class EmployerRules(BaseModel):
    individual_auto_reject_threshold: float = 0.85
    # Rules v1.1 (user decision, Phase 4): an individual sponsor is rejected outright unless the
    # occupation is a listed eligible one, in which case the card goes to a human instead.
    individual_with_eligible_occupation: Literal["REJECT", "REVIEW"] = "REVIEW"
    id_prefix_map: dict[str, Literal["INDIVIDUAL", "COMPANY", "GOVERNMENT"]] = Field(
        default_factory=lambda: {"1": "INDIVIDUAL", "2": "INDIVIDUAL", "7": "COMPANY"}
    )
    rules_file: str = "employer_rules.yaml"
    reference_file: str = "employers_reference.csv"


class OccupationRules(BaseModel):
    model: Literal["ALLOWLIST", "BLOCKLIST"] = "ALLOWLIST"
    fuzzy_threshold: float = 0.88
    review_below_confidence: float = 0.95
    file: str = "occupations.csv"


class DecisionRules(BaseModel):
    auto_approve: bool = False
    require_human_confirmation_on_reject: bool = False
    hard_fail_min_confidence: float = 0.85
    # per-check override; Phase 4 ground truth: every separator-bearing expiry read >= 0.6 was correct
    hard_fail_min_confidence_by_check: dict[str, float] = Field(default_factory=lambda: {"EXPIRY": 0.75})

    def hard_fail_threshold(self, check: str) -> float:
        return self.hard_fail_min_confidence_by_check.get(check, self.hard_fail_min_confidence)


class RetentionRules(BaseModel):
    delete_images_after_final_decision: bool = True
    data_retention_days: int = 365


class RulesConfig(BaseModel):
    version_note: str = ""
    image: ImageRules = Field(default_factory=ImageRules)
    layout: LayoutRules = Field(default_factory=LayoutRules)
    ocr: OCRRules = Field(default_factory=OCRRules)
    checks: ChecksRules = Field(default_factory=ChecksRules)
    nationality: NationalityRules = Field(default_factory=NationalityRules)
    expiry: ExpiryRules = Field(default_factory=ExpiryRules)
    employer: EmployerRules = Field(default_factory=EmployerRules)
    occupation: OccupationRules = Field(default_factory=OccupationRules)
    decision: DecisionRules = Field(default_factory=DecisionRules)
    retention: RetentionRules = Field(default_factory=RetentionRules)


class EmployerKeywordRules(BaseModel):
    government_keywords: list[str] = Field(default_factory=list)
    legal_form_keywords: list[str] = Field(default_factory=list)
    activity_keywords: list[str] = Field(default_factory=list)
    person_name_patterns: list[str] = Field(default_factory=list)


# ---------- CSV row schemas ----------
class OccupationRow(BaseModel):
    code: str = ""
    occupation_ar: str
    occupation_en: str = ""
    category: str = ""
    eligible: bool
    reason: str = ""
    aliases: list[str] = Field(default_factory=list)
    updated_by: str = ""
    updated_at: str = ""

    @field_validator("eligible", mode="before")
    @classmethod
    def _yes_no(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in {"yes", "y", "true", "1", "نعم"}

    @field_validator("aliases", mode="before")
    @classmethod
    def _split(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return v
        return [a.strip() for a in str(v or "").split("|") if a.strip()]


class NationalityRow(BaseModel):
    code: str
    name_ar: str
    name_en: str = ""
    aliases: list[str] = Field(default_factory=list)
    approved: bool = True
    note: str = ""

    @field_validator("approved", mode="before")
    @classmethod
    def _yes_no(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in {"yes", "y", "true", "1", "", "نعم"}

    @field_validator("aliases", mode="before")
    @classmethod
    def _split(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return v
        return [a.strip() for a in str(v or "").split("|") if a.strip()]


class EmployerRefRow(BaseModel):
    employer_id: str = ""
    name_normalized: str = ""
    employer_type: Literal["INDIVIDUAL", "COMPANY", "GOVERNMENT"]
    source: str = ""


class RulesLoadError(Exception):
    pass


FILES = ["rules.yaml", "card_labels.yaml", "employer_rules.yaml", "employers_reference.csv", "occupations.csv", "nationalities.csv"]


def _read_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


class RulesSnapshot:
    """Immutable, validated view of one rules version."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        try:
            self.config = RulesConfig.model_validate(yaml.safe_load(files["rules.yaml"]) or {})
            labels = yaml.safe_load(files["card_labels.yaml"]) or {}
            self.header_ignore: list[str] = [normalize_arabic(x) for x in labels.pop("header_ignore", [])]
            self.card_labels: dict[str, list[str]] = {k: list(v) for k, v in labels.items()}
            self.employer_keywords = EmployerKeywordRules.model_validate(yaml.safe_load(files["employer_rules.yaml"]) or {})
            self.occupations = [OccupationRow.model_validate(r) for r in _read_csv(files["occupations.csv"])]
            self.nationalities = [NationalityRow.model_validate(r) for r in _read_csv(files["nationalities.csv"])]
            self.employer_reference = [EmployerRefRow.model_validate(r) for r in _read_csv(files["employers_reference.csv"]) if r.get("employer_type")]
        except (ValidationError, yaml.YAMLError, KeyError) as e:
            raise RulesLoadError(str(e)) from e
        h = hashlib.sha256()
        for name in FILES:
            h.update(name.encode()); h.update(files[name].encode("utf-8"))
        self.version = h.hexdigest()[:12]
        self.loaded_at = datetime.now(timezone.utc)
        # indexes
        self._occ_exact: dict[str, OccupationRow] = {}
        for row in self.occupations:
            self._occ_exact[normalize_arabic(row.occupation_ar)] = row
            if row.occupation_en:
                self._occ_exact.setdefault(normalize_arabic(row.occupation_en), row)
        self._occ_alias: dict[str, OccupationRow] = {}
        for row in self.occupations:
            for a in row.aliases:
                self._occ_alias.setdefault(normalize_arabic(a), row)
        self._nat_lookup: dict[str, NationalityRow] = {}
        for row in self.nationalities:
            for key in [row.name_ar, row.name_en, row.code, *row.aliases]:
                if key:
                    self._nat_lookup.setdefault(normalize_arabic(key), row)
        self._nat_by_code = {r.code.upper(): r for r in self.nationalities}
        self._emp_by_id = {r.employer_id: r for r in self.employer_reference if r.employer_id}
        self._emp_by_name = {normalize_arabic(r.name_normalized): r for r in self.employer_reference if r.name_normalized}

    # lookups used by engines / pipeline
    def occupation_exact(self, text: str) -> OccupationRow | None:
        return self._occ_exact.get(normalize_arabic(text))

    def occupation_alias(self, text: str) -> OccupationRow | None:
        return self._occ_alias.get(normalize_arabic(text))

    def occupation_candidates(self) -> list[tuple[str, OccupationRow]]:
        out = [(k, v) for k, v in self._occ_exact.items()]
        out += [(k, v) for k, v in self._occ_alias.items()]
        return out

    def nationality_lookup(self, text: str) -> NationalityRow | None:
        return self._nat_lookup.get(normalize_arabic(text))

    def nationality_by_code(self, code: str) -> NationalityRow | None:
        return self._nat_by_code.get((code or "").upper())

    def nationality_keys(self) -> list[tuple[str, NationalityRow]]:
        return list(self._nat_lookup.items())

    def employer_ref_by_id(self, employer_id: str) -> EmployerRefRow | None:
        return self._emp_by_id.get(employer_id)

    def employer_ref_by_name(self, norm_name: str) -> EmployerRefRow | None:
        return self._emp_by_name.get(norm_name)


class RulesRepository:
    """Thread-safe holder of the active snapshot with reload + history."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = Path(config_dir)
        self._lock = threading.RLock()
        self._active: RulesSnapshot | None = None
        self.history: list[tuple[str, datetime]] = []
        self.reload()

    @property
    def active(self) -> RulesSnapshot:
        with self._lock:
            assert self._active is not None
            return self._active

    def reload(self, actor: str = "system") -> RulesSnapshot:
        files = {}
        for name in FILES:
            p = self.config_dir / name
            if not p.exists():
                raise RulesLoadError(f"missing config file: {name}")
            files[name] = p.read_text(encoding="utf-8")
        snap = RulesSnapshot(files)  # raises RulesLoadError -> previous snapshot stays active
        with self._lock:
            self._active = snap
            self.history.append((snap.version, snap.loaded_at))
        return snap

    def replace_file(self, name: str, content: str, actor: str = "system") -> RulesSnapshot:
        """Validate the candidate set BEFORE writing to disk; reject and keep the active snapshot on error."""
        if name not in FILES:
            raise RulesLoadError(f"unknown config file: {name}")
        candidate = dict(self.active.files)
        candidate[name] = content
        snap = RulesSnapshot(candidate)  # validation
        (self.config_dir / name).write_text(content, encoding="utf-8")
        with self._lock:
            self._active = snap
            self.history.append((snap.version, snap.loaded_at))
        return snap

    def upsert_occupation(self, row: dict[str, Any], actor: str) -> RulesSnapshot:
        """Add or replace one occupation row (by Arabic name) and persist the CSV."""
        rows = _read_csv(self.active.files["occupations.csv"])
        key = normalize_arabic(row["occupation_ar"])
        rows = [r for r in rows if normalize_arabic(r["occupation_ar"]) != key]
        new = {
            "code": row.get("code", ""), "occupation_ar": row["occupation_ar"], "occupation_en": row.get("occupation_en", ""),
            "category": row.get("category", ""), "eligible": "Yes" if row.get("eligible") else "No",
            "reason": row.get("reason", ""), "aliases": "|".join(row.get("aliases", []) if isinstance(row.get("aliases"), list) else str(row.get("aliases", "")).split("|")),
            "updated_by": actor, "updated_at": datetime.now(timezone.utc).date().isoformat(),
        }
        rows.append(new)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(new.keys()))
        w.writeheader(); w.writerows(rows)
        return self.replace_file("occupations.csv", buf.getvalue(), actor)
