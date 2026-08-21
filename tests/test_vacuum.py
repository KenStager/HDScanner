"""Tests for reclaiming space after a prune.

Deleting rows does not shrink a SQLite file — it keeps the freed pages for
reuse. After pruning 491,902 rows on 2026-08-20 the database still measured
1.43 GB with 1.26 GB reclaimable, so a prune that only deletes looks broken.
"""

from __future__ import annotations

import sqlite3

import pytest

from hd.db.base import freed_space, maybe_vacuum, sqlite_path


def make_db(path, rows=4000):
    conn = sqlite3.connect(path)
    conn.execute("create table t (id integer primary key, blob text)")
    conn.executemany("insert into t (blob) values (?)", [("x" * 400,) for _ in range(rows)])
    conn.commit()
    conn.close()


def url_for(path):
    return f"sqlite+aiosqlite:///{path}"


# --- which backends this applies to -----------------------------------------

def test_sqlite_url_resolves_to_a_path(tmp_path):
    assert sqlite_path(url_for(tmp_path / "x.db")) == tmp_path / "x.db"


def test_postgres_is_left_alone():
    """Postgres autovacuums, and its VACUUM does not mean the same thing."""
    assert sqlite_path("postgresql+asyncpg://user@host/db") is None
    ran, note = maybe_vacuum("postgresql+asyncpg://user@host/db", 25)
    assert ran is False
    assert "not a SQLite database" in note


def test_memory_database_is_skipped():
    assert sqlite_path("sqlite+aiosqlite:///:memory:") is None


def test_unparseable_url_is_not_fatal():
    assert sqlite_path("!!!not a url!!!") is None


# --- the threshold ----------------------------------------------------------

def test_a_clean_database_is_left_alone(tmp_path):
    db = tmp_path / "clean.db"
    make_db(db)
    ran, note = maybe_vacuum(url_for(db), 25)
    assert ran is False
    assert "below the 25% threshold" in note


def test_a_mostly_empty_database_is_reclaimed(tmp_path):
    db = tmp_path / "wasteful.db"
    make_db(db)
    before = db.stat().st_size

    conn = sqlite3.connect(db)
    conn.execute("delete from t where id > 100")   # free most of the pages
    conn.commit()
    conn.close()

    reclaimable, total = freed_space(url_for(db))
    assert reclaimable / total > 0.25          # precondition for the test
    assert db.stat().st_size == before          # deleting alone shrinks nothing

    ran, note = maybe_vacuum(url_for(db), 25)
    assert ran is True
    assert "reclaimed" in note
    assert db.stat().st_size < before


def test_dry_run_reports_without_rewriting(tmp_path):
    db = tmp_path / "wasteful.db"
    make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("delete from t where id > 100")
    conn.commit()
    conn.close()
    before = db.stat().st_size

    ran, note = maybe_vacuum(url_for(db), 25, dry_run=True)
    assert ran is False
    assert "would vacuum" in note
    assert db.stat().st_size == before


def test_threshold_of_zero_disables_it(tmp_path):
    db = tmp_path / "x.db"
    make_db(db)
    ran, note = maybe_vacuum(url_for(db), 0)
    assert ran is False and note == "vacuum disabled"


def test_data_survives_the_rewrite(tmp_path):
    db = tmp_path / "wasteful.db"
    make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("delete from t where id > 100")
    conn.commit()
    conn.close()

    maybe_vacuum(url_for(db), 25)

    conn = sqlite3.connect(db)
    assert conn.execute("select count(*) from t").fetchone()[0] == 100
    assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_a_locked_database_reports_instead_of_hanging(tmp_path):
    """The 04:00 scan can still be running when the 04:30 prune fires."""
    db = tmp_path / "busy.db"
    make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("delete from t where id > 100")
    conn.commit()
    conn.execute("begin exclusive")   # hold the write lock, as a live scan would
    try:
        ran, note = maybe_vacuum(url_for(db), 25, timeout=0.2)
        assert ran is False
        assert "could not vacuum" in note and "scan may be running" in note
    finally:
        conn.rollback()
        conn.close()


def test_missing_file_is_not_an_error(tmp_path):
    assert freed_space(url_for(tmp_path / "nope.db")) is None


class TestJournalMode:
    """WAL is what lets the resident dashboard and a scheduled scan coexist.

    Under the default rollback journal a reader blocks a writer outright, so a
    dashboard left open would make scans and the nightly VACUUM fail — the
    failure mode being "it quietly stopped collecting", which is the worst one
    for an install nobody is watching.
    """

    async def test_init_puts_sqlite_in_wal(self, tmp_path):
        from sqlalchemy import text

        from hd.config import Settings
        from hd.db.base import Database

        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/wal.db")
        db = Database()
        try:
            await db.init_db(settings)
            async with db.get_engine(settings).connect() as conn:
                mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            assert str(mode).lower() == "wal"
        finally:
            await db.close_db()

    async def test_writers_wait_instead_of_failing_instantly(self, tmp_path):
        """busy_timeout defaults to 0: an overlapping writer would raise at once."""
        from sqlalchemy import text

        from hd.config import Settings
        from hd.db.base import SQLITE_BUSY_TIMEOUT_SECONDS, Database

        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/busy.db")
        db = Database()
        try:
            await db.init_db(settings)
            async with db.get_engine(settings).connect() as conn:
                timeout_ms = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            assert timeout_ms == SQLITE_BUSY_TIMEOUT_SECONDS * 1000
        finally:
            await db.close_db()
