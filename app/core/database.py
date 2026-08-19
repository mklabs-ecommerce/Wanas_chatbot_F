"""Database engine, session factory and declarative base.

Shared infrastructure only. This module knows how to hand out a session; it does not
know about any table. Table definitions and queries live in the owning module's
``repository.py`` (see the boundary rules in ``app/modules/__init__.py``).
"""

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

_url = settings.sqlalchemy_url
_is_sqlite = _url.startswith("sqlite")

# SQLite needs check_same_thread=False because FastAPI serves requests from a
# threadpool; Postgres wants pre-ping instead to survive dropped connections.
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(
    _url,
    connect_args=_connect_args,
    pool_pre_ping=not _is_sqlite,
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    """Declarative base every module's ORM models inherit from."""


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session for use inside repositories: commits, or rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session without an implicit commit."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Create any tables that don't exist yet.

    Modules register their tables simply by having been imported before this runs —
    ``app.main`` imports each module's router, which pulls in its repository/models.
    Good enough for this project's size; swap in Alembic if migrations become a need.
    """
    Base.metadata.create_all(bind=engine)
    tables = sorted(Base.metadata.tables)
    logger.info(
        "Database ready at %s (%d table(s): %s)",
        _url,
        len(tables),
        ", ".join(tables) if tables else "none registered yet",
    )
