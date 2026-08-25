"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
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
    ("products", "upc", "VARCHAR(20)"),
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


# How long a blocked writer waits for the lock before giving up. The default
# is 0: the first scan that overlapped the dashboard would fail instantly.
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _get_engine_kwargs(url: str) -> dict:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {
            "check_same_thread": False,
            # sqlite3's `timeout` is the busy timeout, in seconds.
            "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
        }
    return kwargs


async def _enable_wal(conn) -> None:
    """Switch SQLite to WAL. Recorded in the file header, so it sticks.

    The scanner writes on a schedule and the dashboard is resident, so readers
    and writers now overlap by design. Under the default rollback journal a
    reader blocks a writer outright; under WAL they do not block each other,
    and the nightly VACUUM stops losing races with a running scan.

    Set here rather than per-connection: journal mode is a property of the
    database, and a synchronous PRAGMA in a connect hook leaves work pending on
    aiosqlite's worker thread after the loop it belongs to has gone.
    """
    mode = (await conn.execute(text("PRAGMA journal_mode=WAL"))).scalar()
    if mode and str(mode).lower() != "wal":
        # Network filesystems refuse WAL. Not fatal — it just means the old
        # reader-blocks-writer behaviour, which is what we had before.
        log.warning("Could not enable WAL; concurrent access may contend", journal_mode=mode)


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
            if engine.url.get_backend_name() == "sqlite":
                # First statement in the block: the SQLite driver defers BEGIN
                # until DML, and journal mode cannot change inside a transaction.
                await _enable_wal(conn)
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


# --- reclaiming space --------------------------------------------------------

def sqlite_path(database_url: str) -> Path | None:
    """Filesystem path behind a SQLite URL, or None for any other backend.

    PostgreSQL is deliberately excluded: it autovacuums, and its VACUUM does
    not mean the same thing.
    """
    from sqlalchemy.engine.url import make_url

    try:
        url = make_url(database_url)
    except Exception:
        return None
    if not url.drivername.startswith("sqlite") or not url.database:
        return None
    if url.database == ":memory:":
        return None
    return Path(url.database)


class NotMeasurable(Exception):
    """The file exists but could not be measured — usually a lock."""


def freed_space(database_url: str) -> tuple[int, int] | None:
    """(reclaimable bytes, total bytes) for a SQLite file, or None if not applicable.

    None means "this is not a SQLite database we can act on". A file that
    exists but cannot be read raises NotMeasurable instead, because a locked
    database and a Postgres URL warrant very different messages — reporting a
    busy database as "not SQLite" sends the reader looking in the wrong place.
    """
    import sqlite3

    path = sqlite_path(database_url)
    if path is None or not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            pages = conn.execute("pragma freelist_count").fetchone()[0]
            page_size = conn.execute("pragma page_size").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise NotMeasurable(str(e)) from e
    return pages * page_size, path.stat().st_size


def maybe_vacuum(
    database_url: str, threshold_pct: int, *, dry_run: bool = False, timeout: float = 30.0
) -> tuple[bool, str]:
    """VACUUM when reclaimable space exceeds the threshold. Returns (ran, message).

    A scan may still be running when the nightly maintenance fires — the 04:00
    scan and the 04:30 prune can overlap — so this takes a busy timeout and
    reports a locked database instead of blocking the job or raising.
    """
    import sqlite3

    if threshold_pct <= 0:
        return False, "vacuum disabled"
    try:
        measured = freed_space(database_url)
    except NotMeasurable as e:
        return False, f"could not vacuum ({e}); a scan may be running"
    if measured is None:
        return False, "not a SQLite database — nothing to reclaim"

    reclaimable, total = measured
    pct = (reclaimable / total * 100) if total else 0.0
    summary = f"{reclaimable / 1e6:,.0f} MB reclaimable of {total / 1e6:,.0f} MB ({pct:.0f}%)"
    if pct < threshold_pct:
        return False, f"{summary} — below the {threshold_pct}% threshold"
    if dry_run:
        return False, f"{summary} — would vacuum"

    path = sqlite_path(database_url)
    try:
        conn = sqlite3.connect(path, timeout=timeout)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return False, f"{summary} — could not vacuum ({e}); a scan may be running"
    except sqlite3.Error as e:
        return False, f"{summary} — vacuum failed ({e})"

    after = path.stat().st_size
    return True, f"reclaimed {(total - after) / 1e6:,.0f} MB ({total / 1e6:,.0f} → {after / 1e6:,.0f} MB)"
