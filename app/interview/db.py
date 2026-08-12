"""Connection to the separate interview database.

Deliberately its own engine and session factory. Nothing here shares a transaction with the
candidate database — the two are linked by id and reconciled in ``service.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.interview.models import InterviewBase
from app.migrate import migrate

_settings = get_settings()

_connect_args = (
    {"check_same_thread": False} if _settings.interview_database_url.startswith("sqlite") else {}
)

interview_engine = create_engine(
    _settings.interview_database_url, connect_args=_connect_args, future=True
)

InterviewSession = sessionmaker(
    bind=interview_engine, autoflush=False, expire_on_commit=False, future=True
)


def init_interview_db() -> None:
    InterviewBase.metadata.create_all(interview_engine)
    migrate(interview_engine, InterviewBase.metadata)


def get_interview_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = InterviewSession()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def interview_session_scope() -> Iterator[Session]:
    session = InterviewSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
