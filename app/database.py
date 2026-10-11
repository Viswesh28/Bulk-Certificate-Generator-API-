"""SQLAlchemy engine / session plumbing.

The app never uses a global engine: ``create_app()`` builds one from
``Settings`` and stores it (plus a session factory) on ``app.state``.
That keeps tests trivially isolated (each test gets its own temp database).
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""


def make_engine(database_url: str) -> Engine:
    """Create an engine, applying SQLite-specific pragmas when needed."""
    kwargs: dict = {}
    is_sqlite = database_url.startswith("sqlite")
    if is_sqlite:
        # The worker pool renders certificates in threads; connections may
        # legitimately be used from a thread other than the one that opened
        # them (always sequentially). ``timeout`` is the busy timeout.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}

    engine = create_engine(database_url, **kwargs)

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            # WAL: readers (status polling) never block the writer (workers).
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # Wait instead of failing with "database is locked".
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    # expire_on_commit=False: objects stay readable after commit, which the
    # worker (running in a thread) and the API both rely on.
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def init_db(engine: Engine) -> None:
    """Create tables if they don't exist.

    Deliberate simplification for a take-home project: no Alembic migrations.
    ``Base.metadata.create_all`` is idempotent; in a real deployment the schema
    would be managed with Alembic (see README → Possible improvements).
    """
    from . import models as _models  # noqa: F401  (registers models on Base.metadata)
    assert _models  # keep linters quiet: the import is for its side effect

    Base.metadata.create_all(engine)
