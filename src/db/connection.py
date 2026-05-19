"""
src/db/connection.py
────────────────────────────────────────────────────────────────
Database connection management.

This module provides the single point of entry for all database
access in the project. No other module creates engine or session
objects directly — they import from here.

Backend-agnostic design:
  The application supports any SQLAlchemy-compatible database.
  PostgreSQL (with PostGIS) is used in production. SQLite can
  be used for lightweight testing without Docker.
  Switching is done entirely through the DATABASE_URL in .env —
  no application code needs to change.

  PostgreSQL (production):
    DATABASE_URL=postgresql://user:pass@host:5432/nigeria_health

  SQLite (tests / local quick-start):
    DATABASE_URL=sqlite:///./nigeria_health.db

Connection pooling:
  We use SQLAlchemy's built-in QueuePool for PostgreSQL (the
  default). Pool settings are kept conservative — this app is
  primarily read-heavy with infrequent ETL writes.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── Module-level singletons ───────────────────────────────────────
# These are created once when the module is first imported and
# reused for the lifetime of the process.
_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def _build_engine(database_url: str) -> Engine:
    """
    Create a SQLAlchemy Engine from a database URL.

    Pool and echo settings differ between development and
    production to balance visibility and performance.

    Parameters
    ----------
    database_url : str
        A valid SQLAlchemy connection string.

    Returns
    -------
    Engine
    """
    is_sqlite = database_url.startswith("sqlite")
    is_dev    = settings.is_development

    if is_sqlite:
        # SQLite does not support connection pooling in the same way.
        # connect_args={"check_same_thread": False} is required when
        # SQLite is used with multiple threads (e.g. inside FastAPI).
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            echo=is_dev,    # Log SQL in development only
        )
        logger.info("Using SQLite database (development/testing mode)")

    else:
        # PostgreSQL — use connection pooling.
        # pool_pre_ping=True checks that the connection is alive before
        # handing it to the application, preventing "connection closed"
        # errors after idle periods.
        engine = create_engine(
            database_url,
            pool_size=5,          # Connections kept open permanently
            max_overflow=10,      # Extra connections allowed under load
            pool_pre_ping=True,
            pool_recycle=3600,    # Recycle connections after 1 hour
            echo=False,           # Never log raw SQL with credentials
        )
        logger.info(
            "Using PostgreSQL database: %s",
            # Log only the host:port/db — never username or password
            database_url.split("@")[-1] if "@" in database_url else database_url,
        )

    return engine


def _maybe_enable_postgis(engine: Engine) -> None:
    """
    Enable the PostGIS extension if we're on PostgreSQL.

    PostGIS must be enabled once per database. Running
    CREATE EXTENSION IF NOT EXISTS postgis is idempotent —
    safe to call on every startup.

    Parameters
    ----------
    engine : Engine
        An active SQLAlchemy engine.
    """
    if engine.dialect.name != "postgresql":
        return

    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
            conn.commit()
        logger.info("PostGIS extension confirmed active")
    except Exception as exc:
        # Not fatal — PostGIS may already exist or user may lack privileges.
        # Spatial queries will fail later if PostGIS truly isn't available.
        logger.warning("Could not ensure PostGIS extension: %s", exc)


def get_engine() -> Engine:
    """
    Return the module-level database engine, creating it on first call.

    This follows the singleton pattern — the engine (and its connection
    pool) is expensive to create, so we do it once and reuse it.

    Returns
    -------
    Engine
        A ready-to-use SQLAlchemy Engine.
    """
    global _engine

    if _engine is None:
        _engine = _build_engine(settings.database_url)
        _maybe_enable_postgis(_engine)
        logger.debug("Database engine initialised")

    return _engine


def get_session_factory() -> sessionmaker:
    """
    Return the module-level session factory, creating it on first call.

    Returns
    -------
    sessionmaker
        A factory that produces Session objects bound to our engine.
    """
    global _SessionFactory

    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,  # Keep objects usable after commit
        )

    return _SessionFactory


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """
    Provide a transactional database session as a context manager.

    Usage:
        with get_db_session() as session:
            results = session.execute(text("SELECT 1")).fetchall()

    The session is automatically committed on clean exit and rolled
    back on exception. Either way, it is always closed on exit.

    Yields
    ------
    Session
        An active SQLAlchemy Session.

    Raises
    ------
    Re-raises any exception after rolling back the transaction.
    """
    session_factory = get_session_factory()
    session = session_factory()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session_dependency() -> Generator[Session, None, None]:
    """
    FastAPI dependency that provides a database session per request.

    This is a generator function (not a context manager) because
    FastAPI's dependency injection system expects a generator.

    Usage in a route:
        @router.get("/example")
        def example(db: Session = Depends(get_db_session_dependency)):
            ...

    Yields
    ------
    Session
        An active database session. Closed automatically after
        the request completes.
    """
    session_factory = get_session_factory()
    session = session_factory()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def verify_connection() -> bool:
    """
    Test that the database is reachable and responding.

    Runs a trivial query (SELECT 1) to confirm connectivity.
    Used at application startup to fail fast on misconfiguration.

    Returns
    -------
    bool
        True if the database responded correctly, False otherwise.
    """
    try:
        with get_engine().connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
            if result == 1:
                logger.info("Database connectivity verified ✓")
                return True
            logger.error("Database returned unexpected response: %s", result)
            return False
    except Exception as exc:
        logger.error("Database connection failed: %s", exc)
        return False


def dispose_engine() -> None:
    """
    Close all connections in the pool and dispose the engine.

    Call this during application shutdown or in test teardown to
    ensure all connections are cleanly closed.
    """
    global _engine, _SessionFactory

    if _engine is not None:
        _engine.dispose()
        _engine = None
        _SessionFactory = None
        logger.info("Database engine disposed")
