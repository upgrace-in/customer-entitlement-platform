"""Async SQLite database setup via SQLAlchemy + aiosqlite.

DATA_MODEL_RATIONALE
====================
*   **SQLite + aiosqlite** — Chosen for zero-config local development and
    single-binary deployments.  The async driver (``aiosqlite``) lets us share
    the same ``asyncio`` event loop that FastAPI uses, avoiding thread-pool
    overhead for every query.

*   **Idempotency records** — Stored in the database (rather than Redis) to
    keep the dependency footprint small.  A TTL column (``expires_at``) lets a
    periodic sweep or ``DELETE WHERE expires_at < now()`` reclaim space.

*   **Audit log** — Append-only table for compliance and debugging.  Indexed on
    ``correlation_id`` so a single API request's side-effects can be traced, and
    on ``created_at`` for time-range queries.

*   **Webhook subscriptions** — First-class table so subscription CRUD lives in
    the same transactional boundary as the rest of the domain data.  The
    ``events`` column stores a JSON array of event-type strings; for the
    expected cardinality (tens of subscriptions) a JSON column is simpler than a
    join table.

*   **Indexes** — Explicitly declared rather than relying on ORM defaults so
    their intent is documented and migration tools can diff them reliably.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine & session factory (module-level singletons, initialised lazily)
# ---------------------------------------------------------------------------

DATABASE_URL: str = "sqlite+aiosqlite:///./entitlements.db"

_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    """Return the module-level engine, creating it on first call."""
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False},
        )
        # Enable WAL mode for better concurrent-read performance.
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_session_factory  # noqa: PLW0603
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


# ---------------------------------------------------------------------------
# Declarative base & table definitions
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""


class IdempotencyRecord(Base):
    """Stores idempotent operation results keyed by a client-provided token."""

    __tablename__ = "idempotency_records"

    key: str = Column(String(256), primary_key=True, nullable=False)
    response: str = Column(Text, nullable=False)
    created_at: datetime = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: datetime = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<IdempotencyRecord key={self.key!r}>"


class AuditLog(Base):
    """Immutable audit trail of significant actions."""

    __tablename__ = "audit_log"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    correlation_id: str = Column(String(64), nullable=True)
    user_id: str = Column(String(256), nullable=True)
    action: str = Column(String(128), nullable=False)
    resource_type: str = Column(String(128), nullable=False)
    resource_id: str = Column(String(256), nullable=False)
    details: str = Column(Text, nullable=True)
    created_at: datetime = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_audit_log_correlation_id", "correlation_id"),
        Index("ix_audit_log_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} action={self.action!r} "
            f"resource={self.resource_type}/{self.resource_id}>"
        )


class WebhookSubscription(Base):
    """Registered webhook endpoint and the events it subscribes to."""

    __tablename__ = "webhook_subscriptions"

    id: str = Column(String(64), primary_key=True, nullable=False)
    url: str = Column(String(2048), nullable=False)
    events: str = Column(Text, nullable=False, doc="JSON array of event-type strings.")
    secret: str = Column(String(256), nullable=False)
    active: bool = Column(Boolean, nullable=False, default=True)
    created_at: datetime = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<WebhookSubscription id={self.id!r} url={self.url!r}>"


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def init_db(*, engine: AsyncEngine | None = None) -> None:
    """Create all tables if they do not already exist.

    Parameters
    ----------
    engine:
        Optional engine override — useful in tests to point at an in-memory DB.
    """
    target_engine = engine or _get_engine()
    async with target_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialised (url=%s)", target_engine.url)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession`` and cleans up.

    Usage::

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager variant of :func:`get_db` for non-FastAPI callers."""
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
