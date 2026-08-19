"""Tests for database provisioning.

Three things can be wrong with a database and each needs a different fix: the
driver is not installed, the server cannot be reached, or the database does not
exist yet. Collapsing them into "connection failed" leaves the user guessing,
so each is pinned. Redaction is pinned too — a Postgres URL carries a password
and setup prints the URL back on failure.
"""

from __future__ import annotations

import pytest

from hd.config import Settings
from hd import setup_database as db
from hd.setup_database import (
    SQLITE_DEFAULT,
    check_connection,
    create_database,
    describe,
    driver_for,
    initialise_schema,
    redact,
)

PG = "postgresql+asyncpg://ken:hunter2@db.example.com:5432/hd"


class TestRedaction:
    def test_password_is_hidden(self):
        out = redact(PG)
        assert "hunter2" not in out
        assert "ken" in out and "db.example.com" in out

    def test_sqlite_is_unchanged_in_substance(self):
        assert "dev.db" in redact(SQLITE_DEFAULT)

    def test_unparseable_input_does_not_raise(self):
        assert redact("not a url") == "not a url"


class TestDescribe:
    def test_sqlite_names_the_file(self):
        assert describe(SQLITE_DEFAULT) == "SQLite file ./dev.db"

    def test_postgres_names_host_and_db_without_password(self):
        out = describe(PG)
        assert "hd" in out and "db.example.com" in out
        assert "hunter2" not in out


class TestDriverFor:
    def test_postgres_needs_asyncpg_from_an_extra(self):
        assert driver_for(PG) == ("asyncpg", "postgres")

    def test_sqlite_driver_needs_no_extra(self):
        module, extra = driver_for(SQLITE_DEFAULT)
        assert module == "aiosqlite" and extra is None


class TestCheckConnection:
    async def test_sqlite_in_a_writable_dir_succeeds(self, tmp_path):
        check = await check_connection(f"sqlite+aiosqlite:///{tmp_path}/x.db")
        assert check.ok

    async def test_missing_directory_is_reported_with_a_fix(self, tmp_path):
        check = await check_connection(f"sqlite+aiosqlite:///{tmp_path}/nope/x.db")
        assert not check.ok and check.fix

    async def test_missing_driver_names_the_install_command(self, monkeypatch):
        monkeypatch.setattr(db, "_driver_installed", lambda m: False)
        check = await check_connection(PG)
        assert not check.ok
        assert "asyncpg" in check.detail
        assert "[postgres]" in check.fix

    async def test_invalid_url_is_rejected(self):
        check = await check_connection("://////")
        assert not check.ok

    async def test_missing_database_is_flagged_as_creatable(self, monkeypatch):
        """Distinct from unreachable — this is the one setup can fix itself."""
        monkeypatch.setattr(db, "_driver_installed", lambda m: True)

        class _Engine:
            def connect(self):
                raise RuntimeError('database "hd" does not exist')

            async def dispose(self):
                return None

        monkeypatch.setattr(db, "create_async_engine", lambda *a, **k: _Engine(), raising=False)
        import sqlalchemy.ext.asyncio as sa_async

        monkeypatch.setattr(sa_async, "create_async_engine", lambda *a, **k: _Engine())
        check = await check_connection(PG)
        assert check.missing_database is True


class TestCreateDatabase:
    async def test_sqlite_needs_no_creation(self):
        assert (await create_database(SQLITE_DEFAULT)).ok

    async def test_url_without_a_database_name_is_rejected(self):
        check = await create_database("postgresql+asyncpg://u:p@host")
        assert not check.ok


class TestInitialiseSchema:
    async def test_creates_every_table_the_models_define(self, tmp_path):
        """0 to 1: an empty file must end up with the full schema."""
        from hd.db.models import Base

        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/fresh.db")
        tables = await initialise_schema(settings)
        assert set(Base.metadata.tables).issubset(set(tables))

    async def test_is_idempotent(self, tmp_path):
        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/twice.db")
        first = await initialise_schema(settings)
        second = await initialise_schema(settings)
        assert first == second


class TestSchemaPortability:
    """Guards two defects that only appeared on PostgreSQL.

    SQLite is permissive enough to hide both, so these assert on the schema
    declaration rather than on runtime behaviour against a live server.
    """

    def test_every_timestamp_column_is_timezone_aware(self):
        """The code writes UTC-aware datetimes.

        Declared naive, PostgreSQL rejects every insert with "can't subtract
        offset-naive and offset-aware datetimes" while SQLite silently accepts
        them — so the whole write path was broken on Postgres only.
        """
        from sqlalchemy import DateTime

        from hd.db.models import Base

        naive = [
            f"{table}.{col.name}"
            for table, t in Base.metadata.tables.items()
            for col in t.columns
            if isinstance(col.type, DateTime) and not col.type.timezone
        ]
        assert naive == [], f"timezone-naive timestamp columns: {naive}"

    async def test_init_db_is_safe_to_run_twice_on_a_populated_schema(self, tmp_path):
        """Column migrations must not roll back create_all beside them.

        They previously shared one transaction. PostgreSQL aborts the entire
        transaction when any statement in it fails, so a redundant ALTER on an
        already-current schema discarded the tables that had just been created.
        """
        from hd.db.base import close_db, init_db
        from hd.db.models import Base

        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/twice.db")
        await init_db(settings)
        await close_db()
        await init_db(settings)
        await close_db()

        tables = await initialise_schema(settings)
        assert set(Base.metadata.tables).issubset(set(tables))
