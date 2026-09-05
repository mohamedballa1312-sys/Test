"""SQLAlchemy models (Architecture §7). Sensitive columns are encrypted at rest via EncryptedText."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class EncryptedText(TypeDecorator):
    """Transparent AES-256-GCM column encryption."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from app.core.security import get_encryptor
        return get_encryptor().encrypt(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from app.core.security import get_encryptor
        return get_encryptor().decrypt(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="OPERATOR")  # OPERATOR|REVIEWER|RULES_ADMIN|AUDITOR
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")  # QUEUED|PROCESSING|DONE|FAILED
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    rules_version: Mapped[str | None] = mapped_column(String(16))
    requesting_companies_json: Mapped[str] = mapped_column(Text, default="[]")   # companies requesting the permit
    project_json: Mapped[str] = mapped_column(Text, default="{}")                # name/location/start/end for the permit form
    permit_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    documents: Mapped[list["Document"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(64))
    page_no: Mapped[int] = mapped_column(Integer, default=1)
    image_path: Mapped[str | None] = mapped_column(String(500))       # encrypted blob on disk; None once deleted
    image_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality_score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")  # QUEUED|PROCESSING|DONE|ERROR
    error_msg: Mapped[str | None] = mapped_column(Text)
    ocr_provider: Mapped[str | None] = mapped_column(String(32))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duplicate_of: Mapped[int | None] = mapped_column(Integer)
    iqama_no_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    doc_type: Mapped[str] = mapped_column(String(16), default="IQAMA")           # IQAMA | NATIONAL_ID
    company_final: Mapped[str | None] = mapped_column(Text)                       # company name used on the permit form
    company_source: Mapped[str | None] = mapped_column(String(16))                # CARD | AUTO | MANUAL | NATIONAL_ID
    extraction_json: Mapped[str | None] = mapped_column(EncryptedText)  # full ExtractionResult (raw lines incl.)
    batch: Mapped[Batch] = relationship(back_populates="documents")
    fields: Mapped[list["ExtractedField"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    decisions: Mapped[list["DecisionRow"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    reviews: Mapped[list["Review"]] = relationship(back_populates="document", cascade="all, delete-orphan")


SENSITIVE_FIELDS = {"iqama_no", "name_ar", "name_en", "birth_date", "employer_id"}


class ExtractedField(Base):
    __tablename__ = "extracted_fields"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    field_name: Mapped[str] = mapped_column(String(32), index=True)
    raw_text: Mapped[str | None] = mapped_column(EncryptedText)
    normalized_value: Mapped[str | None] = mapped_column(EncryptedText)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    bbox_x: Mapped[int | None] = mapped_column(Integer)
    bbox_y: Mapped[int | None] = mapped_column(Integer)
    bbox_w: Mapped[int | None] = mapped_column(Integer)
    bbox_h: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), default="ocr")
    note: Mapped[str | None] = mapped_column(String(200))
    corrected_by: Mapped[str | None] = mapped_column(String(64))
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    document: Mapped[Document] = relationship(back_populates="fields")


class DecisionRow(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), index=True)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    triggers_json: Mapped[str] = mapped_column(Text, default="[]")
    recommendation: Mapped[str | None] = mapped_column(String(24))
    checks_json: Mapped[str] = mapped_column(EncryptedText, default="[]")
    rules_version: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[str] = mapped_column(String(64), default="system")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)  # set by a human review
    document: Mapped[Document] = relationship(back_populates="decisions")


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    reviewer: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_status: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    previous_decision_id: Mapped[int | None] = mapped_column(Integer)
    new_decision_id: Mapped[int | None] = mapped_column(Integer)
    document: Mapped[Document] = relationship(back_populates="reviews")


class RulesVersion(Base):
    __tablename__ = "rules_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(16), index=True)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    loaded_by: Mapped[str] = mapped_column(String(64), default="system")
    files_snapshot_json: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    """Append-only: the application never issues UPDATE/DELETE against this table."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details_json: Mapped[str] = mapped_column(Text, default="{}")  # PII masked before write
    ip: Mapped[str | None] = mapped_column(String(64))
