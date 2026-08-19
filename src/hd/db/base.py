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

# Indexes have the same gap as columns: create_all adds them only alongside a
# table it creates, never to one that already exists.
_INDEX_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("ix_snapshot_store_item_ts", "store_snapshots", "store_id, item_id, ts"),
    ("ix_snapshot_ts", "store_snapshots", "ts"),
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

        def _reflect(sync_conn) -> dict[str, set[str]]:
            insp = inspect(sync_conn)
            return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}

        async with engine.connect() as conn:
            existing = await conn.run_sync(_reflect)

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

        for index, table, columns in _INDEX_MIGRATIONS:
            if table not in existing:
                continue
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({columns})")
                    )
            except Exception as exc:  # noqa: BLE001 - migration is best effort
                log.warning("Could not create index", index=index, error=str(exc)[:120])

        await self._ensure_enum_values(engine)

    async def _ensure_enum_values(self, engine) -> None:
        """Add enum members PostgreSQL cannot learn from create_all.

        A database created before an AlertType member was added keeps the old
        enum, and the first alert of that kind fails with "invalid input value
        for enum". ALTER TYPE ... ADD VALUE cannot run inside a transaction
        block, so this takes an AUTOCOMMIT connection rather than begin().
        SQLite stores the enum as a string and needs none of it.
        """
        if engine.dialect.name != "postgresql":
            return

        from hd.db.models import AlertType

        try:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                for member in AlertType:
                    await conn.execute(
                        text(
                            "ALTER TYPE alerttype ADD VALUE IF NOT EXISTS "
                            f"'{member.name}'"
                        )
                    )
        except Exception as exc:  # noqa: BLE001 - best effort, fresh installs need none
            log.warning("Could not reconcile alerttype enum", error=str(exc)[:120])

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
