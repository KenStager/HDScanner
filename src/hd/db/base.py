"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hd.config import Settings
from hd.db.models import Base
from hd.logging import get_logger

log = get_logger("db.base")

# Columns added after the original schema. create_all only creates missing
# tables, never missing columns on tables that already exist, so these are
# applied by hand for databases predating each addition.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("products", "image_url", "TEXT"),
    ("stores", "city", "VARCHAR(100)"),
    ("store_snapshots", "clearance_value", "NUMERIC(10,2)"),
    ("store_snapshots", "clearance_dollar_off", "NUMERIC(10,2)"),
    ("store_snapshots", "clearance_percentage_off", "INTEGER"),
)


def _get_engine_kwargs(url: str) -> dict:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


class Database:
    """Holds engine and session factory as instance state."""

    def __init__(self) -> None:
        self._engine = None
        self._session_factory = None

    def get_engine(self, settings: Settings | None = None):
        if self._engine is None:
            if settings is None:
                settings = Settings()
            self._engine = create_async_engine(
                settings.database_url,
                echo=False,
                **_get_engine_kwargs(settings.database_url),
            )
        return self._engine

    def get_session_factory(self, settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            engine = self.get_engine(settings)
            self._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return self._session_factory

    @asynccontextmanager
    async def get_session(self, settings: Settings | None = None) -> AsyncGenerator[AsyncSession, None]:
        factory = self.get_session_factory(settings)
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def init_db(self, settings: Settings | None = None) -> None:
        """Create tables, then apply additive column migrations.

        Each statement gets its own transaction. PostgreSQL aborts the whole
        surrounding transaction when any statement in it fails, so sharing one
        would let a redundant ALTER roll back the create_all beside it — which
        is how a fresh Postgres database ended up with no tables at all while
        SQLite, which tolerates it, appeared to work.

        Existing columns are detected rather than discovered by failure, so the
        common path raises nothing.
        """
        engine = self.get_engine(settings)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with engine.connect() as conn:
            existing = await conn.run_sync(
                lambda sync_conn: {
                    table: {c["name"] for c in inspect(sync_conn).get_columns(table)}
                    for table in inspect(sync_conn).get_table_names()
                }
            )

        for table, column, col_type in _COLUMN_MIGRATIONS:
            if column in existing.get(table, set()):
                continue
            if table not in existing:
                continue
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                log.info("Added column", table=table, column=column)
            except Exception as exc:  # noqa: BLE001 - migration is best effort
                log.warning(
                    "Could not add column", table=table, column=column, error=str(exc)[:120]
                )

    async def close_db(self) -> None:
        """Dispose of the engine."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


# Default instance + backward-compatible module-level functions
_default = Database()


def get_engine(settings: Settings | None = None):
    return _default.get_engine(settings)


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    return _default.get_session_factory(settings)


def get_session(settings: Settings | None = None) -> AsyncGenerator[AsyncSession, None]:
    return _default.get_session(settings)


async def init_db(settings: Settings | None = None) -> None:
    return await _default.init_db(settings)


async def close_db() -> None:
    return await _default.close_db()
