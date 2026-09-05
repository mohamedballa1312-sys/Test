from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Base

_engine = None
_Session: sessionmaker | None = None


def init_db(url: str | None = None):
    global _engine, _Session
    url = url or get_settings().effective_db_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    _engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _fk_on(dbapi_con, _):
            dbapi_con.execute("PRAGMA foreign_keys=ON")
            dbapi_con.execute("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(_engine)
    _ensure_columns(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _ensure_columns(engine) -> None:
    """Additive migration for SQLite/Postgres: add columns that exist in the models but not in the DB yet."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as con:
        for table in Base.metadata.sorted_tables:
            if table.name not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ctype = col.type.compile(dialect=engine.dialect)
                con.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ctype}'))


def get_engine():
    if _engine is None:
        init_db()
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _Session is None:
        init_db()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def reset_db() -> None:
    """Testing helper."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
