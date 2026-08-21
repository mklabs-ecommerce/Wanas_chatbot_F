"""Database engine, session factory and declarative base.

Shared infrastructure only. This module knows how to hand out a session; it does not
know about any table. Table definitions and queries live in the owning module's
``repository.py`` (see the boundary rules in ``app/modules/__init__.py``).
"""

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
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
    _add_missing_columns()
    tables = sorted(Base.metadata.tables)
    logger.info(
        "Database ready at %s (%d table(s): %s)",
        _url,
        len(tables),
        ", ".join(tables) if tables else "none registered yet",
    )


def _add_missing_columns() -> None:
    """Add columns that were introduced after a table already existed.

    ``create_all`` creates missing *tables* but never alters an existing one, so a new
    column would be invisible on any database that predates it - including the one this
    project has been developing against. This closes that gap for the simple case
    (nullable column with a default), which is all this project has needed.

    SQLite only. It builds ``ALTER TABLE ... ADD COLUMN`` by hand with a type string
    compiled for the current dialect, and the ``DEFAULT 0`` it writes for a boolean is
    invalid on Postgres (which wants ``FALSE``). There is no equivalent gap to close on
    Postgres in this project: every environment pointed at one starts from an empty
    database, so ``create_all`` alone gives it every column already.

    Not a migration system. If a column ever needs backfilling, a type change or a
    rename, that is the point to bring in Alembic rather than to extend this.
    """
    if not _is_sqlite:
        return
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for name, table in Base.metadata.tables.items():
        if name not in existing_tables:
            continue  # create_all just made it, with every column
        have = {column["name"] for column in inspector.get_columns(name)}
        for column in table.columns:
            if column.name in have:
                continue
            kind = column.type.compile(dialect=engine.dialect)
            # A NOT NULL boolean column (e.g. Conversation.owner_active) needs a real 0,
            # not NULL - SQLite stores bool as an integer affinity, so this is the same
            # backfill the INTEGER case already gets.
            type_name = str(column.type).upper()
            default = "0" if type_name.startswith("INTEGER") or type_name.startswith("BOOLEAN") else "NULL"
            statement = ("ALTER TABLE " + name + " ADD COLUMN " + column.name + " "
                         + kind + " DEFAULT " + default)
            logger.info("Adding missing column %s.%s", name, column.name)
            with engine.begin() as connection:
                connection.execute(text(statement))
