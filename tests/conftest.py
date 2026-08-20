"""Test fixtures.

Every test runs against a throwaway SQLite file and never touches the network, so the
suite is safe to run repeatedly without spending Gemini or OpenRouter quota.
"""

import os

# Point the app at a temporary database before anything imports app.core.config, whose
# settings object is built at import time.
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    """Give each test its own SQLite file and a matching engine/session factory."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core import database

    url = "sqlite:///" + (tmp_path / "test.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", session_factory)

    # Import the modules that own tables so create_all sees them.
    from app.modules.admin.auth import repository as admin_auth_repository  # noqa: F401
    from app.modules.chat import repository  # noqa: F401
    from app.modules.engagement import repository as engagement_repository  # noqa: F401
    from app.modules.support import repository as support_repository  # noqa: F401

    database.Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clear_llm_cooldowns():
    """Quota cooldowns are module-level state; leaking them between tests hides bugs."""
    from app.integrations import llm

    llm.reset_cooldowns()
    yield
    llm.reset_cooldowns()
