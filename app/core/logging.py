"""structlog configuration with a PII-masking processor applied to every event."""
from __future__ import annotations

import logging
import sys

import structlog

from app.core.security import mask_pii_text


def _mask_processor(_logger, _method, event_dict):
    for k, v in list(event_dict.items()):
        if isinstance(v, str):
            event_dict[k] = mask_pii_text(v)
        elif isinstance(v, dict):
            event_dict[k] = {kk: (mask_pii_text(vv) if isinstance(vv, str) else vv) for kk, vv in v.items()}
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _mask_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "iqama"):
    return structlog.get_logger(name)
