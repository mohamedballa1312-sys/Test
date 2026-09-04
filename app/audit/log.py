"""Append-only audit trail. Details are PII-masked before they are written."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import mask_pii_text
from app.db.models import AuditLog


def _mask(obj: Any) -> Any:
    if isinstance(obj, str):
        return mask_pii_text(obj)
    if isinstance(obj, dict):
        return {k: _mask(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v) for v in obj]
    return obj


def record(session: Session, actor: str, action: str, entity_type: str | None = None, entity_id: Any = None,
           details: dict | None = None, ip: str | None = None) -> None:
    session.add(AuditLog(actor=actor or "system", action=action, entity_type=entity_type,
                         entity_id=str(entity_id) if entity_id is not None else None,
                         details_json=json.dumps(_mask(details or {}), ensure_ascii=False), ip=ip))
