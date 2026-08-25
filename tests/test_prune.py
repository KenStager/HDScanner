"""Tests for the snapshot pruning functionality."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, func, delete

from hd.config import Settings
from hd.db.base import Database
from hd.db.models import Base, StoreSnapshot


@pytest.fixture
def prune_settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        stores="2619",
        brands="Milwaukee",
        product_line_filters="M12,M18",
        store_raw_json=False,
        snapshot_retention_days=90,
    )


@pytest_asyncio.fixture
async def seeded_prune_settings(prune_settings: Settings) -> Settings:
    """Initialize DB with snapshots at various ages."""
    from hd.db import base as db_base

    db_base._default = Database()
    await db_base._default.init_db(prune_settings)

    now = datetime.now(timezone.utc)
    async with db_base._default.get_session(prune_settings) as session:
        # Recent snapshot (10 days old)
        session.add(StoreSnapshot(
            store_id="2619", item_id="100001",
            ts=now - timedelta(days=10),
            price_value=Decimal("199.00"),
            in_stock=True,
        ))
        # Old snapshot (100 days old)
        session.add(StoreSnapshot(
            store_id="2619", item_id="100001",
            ts=now - timedelta(days=100),
            price_value=Decimal("249.00"),
            in_stock=True,
        ))
        # Very old snapshot (200 days old)
        session.add(StoreSnapshot(
            store_id="2619", item_id="100002",
            ts=now - timedelta(days=200),
            price_value=Decimal("149.00"),
            in_stock=False,
        ))

    yield prune_settings
    await db_base._default.close_db()


class TestPrune:
    async def test_prune_deletes_old_snapshots(self, seeded_prune_settings: Settings):
        """Snapshots older than 90 days should be deleted, recent preserved."""
        from hd.db import base as db_base

        cutoff = datetime.now(timezone.utc) - timedelta(days=90)

        async with db_base._default.get_session(seeded_prune_settings) as session:
            # Delete old
            await session.execute(
                delete(StoreSnapshot).where(StoreSnapshot.ts < cutoff)
            )

        async with db_base._default.get_session(seeded_prune_settings) as session:
            result = await session.execute(
                select(func.count()).select_from(StoreSnapshot)
            )
            remaining = result.scalar()

        assert remaining == 1  # only the 10-day-old snapshot survives

    async def test_prune_preserves_recent(self, seeded_prune_settings: Settings):
        """All rows within retention window should not be deleted."""
        from hd.db import base as db_base

        # Use very large retention: nothing should be deleted
        cutoff = datetime.now(timezone.utc) - timedelta(days=365)

        async with db_base._default.get_session(seeded_prune_settings) as session:
            await session.execute(
                delete(StoreSnapshot).where(StoreSnapshot.ts < cutoff)
            )

        async with db_base._default.get_session(seeded_prune_settings) as session:
            result = await session.execute(
                select(func.count()).select_from(StoreSnapshot)
            )
            remaining = result.scalar()

        assert remaining == 3  # all preserved

    async def test_prune_dry_run(self, seeded_prune_settings: Settings):
        """Dry run should count eligible rows but not delete them."""
        from hd.db import base as db_base

        cutoff = datetime.now(timezone.utc) - timedelta(days=90)

        async with db_base._default.get_session(seeded_prune_settings) as session:
            count_result = await session.execute(
                select(func.count()).select_from(StoreSnapshot).where(
                    StoreSnapshot.ts < cutoff
                )
            )
            eligible = count_result.scalar()

        # Don't delete — just count
        assert eligible == 2

        # Verify nothing was deleted
        async with db_base._default.get_session(seeded_prune_settings) as session:
            result = await session.execute(
                select(func.count()).select_from(StoreSnapshot)
            )
            remaining = result.scalar()

        assert remaining == 3

    async def test_prune_custom_retention(self, seeded_prune_settings: Settings):
        """Custom 30-day retention should delete rows older than 30 days."""
        from hd.db import base as db_base

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)

        async with db_base._default.get_session(seeded_prune_settings) as session:
            await session.execute(
                delete(StoreSnapshot).where(StoreSnapshot.ts < cutoff)
            )

        async with db_base._default.get_session(seeded_prune_settings) as session:
            result = await session.execute(
                select(func.count()).select_from(StoreSnapshot)
            )
            remaining = result.scalar()

        assert remaining == 1  # only 10-day-old snapshot survives


# --- raw response retention -------------------------------------------------
#
# Nothing had ever deleted this directory: it reached 3,559 files and 353 MB in
# six days, accumulating at ~59 MB/day.

import time
from pathlib import Path

from hd.cli import prune_raw_responses


def _raw_settings(tmp_path, **kw):
    from hd.config import Settings

    base = dict(_env_file=None, raw_json_dir=str(tmp_path / "raw"), raw_retention_days=7)
    base.update(kw)
    return Settings(**base)


def _write(directory: Path, name: str, age_days: float, size: int = 100) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("x" * size)
    when = time.time() - age_days * 86400
    import os

    os.utime(path, (when, when))
    return path


def test_old_files_go_and_recent_ones_stay(tmp_path):
    s = _raw_settings(tmp_path)
    raw = Path(s.raw_json_dir)
    old = _write(raw, "old.json", 30)
    fresh = _write(raw, "fresh.json", 1)

    files, freed = prune_raw_responses(s)
    assert files == 1 and freed == 100
    assert not old.exists()
    assert fresh.exists()


def test_dry_run_deletes_nothing_but_reports_the_same_total(tmp_path):
    s = _raw_settings(tmp_path)
    raw = Path(s.raw_json_dir)
    old = _write(raw, "old.json", 30)

    files, freed = prune_raw_responses(s, dry_run=True)
    assert files == 1 and freed == 100
    assert old.exists()


def test_zero_retention_keeps_everything(tmp_path):
    s = _raw_settings(tmp_path, raw_retention_days=0)
    _write(Path(s.raw_json_dir), "ancient.json", 900)
    assert prune_raw_responses(s) == (0, 0)


def test_missing_directory_is_not_an_error(tmp_path):
    s = _raw_settings(tmp_path)
    assert prune_raw_responses(s) == (0, 0)


def test_non_json_files_are_left_alone(tmp_path):
    s = _raw_settings(tmp_path)
    raw = Path(s.raw_json_dir)
    keep = _write(raw, "notes.txt", 90)
    prune_raw_responses(s)
    assert keep.exists()


# --- slimming ----------------------------------------------------------------
#
# A snapshot row is the record plus the receipt it was parsed from, and the
# receipt is ~71% of the database. Slimming drops receipts at age and keeps
# records, so retention can hold history for years without holding the blobs.

from hd.cli import slim_snapshots


@pytest_asyncio.fixture
async def slim_seeded(prune_settings: Settings):
    """Rows with receipts at 10, 100, and 200 days old."""
    from hd.db import base as db_base

    db_base._default = Database()
    await db_base._default.init_db(prune_settings)

    now = datetime.now(timezone.utc)
    async with db_base._default.get_session(prune_settings) as session:
        for item, age in (("100001", 10), ("100001", 100), ("100002", 200)):
            session.add(StoreSnapshot(
                store_id="2619", item_id=item,
                ts=now - timedelta(days=age),
                price_value=Decimal("199.00"),
                in_stock=True,
                raw_json={"itemId": item, "padding": "x" * 50},
            ))

    yield prune_settings, now
    await db_base._default.close_db()


async def _rows_by_age(settings):
    from hd.db import base as db_base

    async with db_base._default.get_session(settings) as session:
        rows = (await session.execute(
            select(StoreSnapshot).order_by(StoreSnapshot.ts.desc())
        )).scalars().all()
        return [(r.price_value, r.raw_json) for r in rows]


class TestSlim:
    async def test_band_rows_lose_receipts_and_keep_records(self, slim_seeded):
        settings, now = slim_seeded
        from hd.db import base as db_base

        async with db_base._default.get_session(settings) as session:
            rows, size = await slim_snapshots(session, 30, 180, now=now)

        assert rows == 1 and size > 0
        ten, hundred, two_hundred = await _rows_by_age(settings)
        assert ten[1] is not None                      # too young to slim
        assert hundred[1] is None                      # slimmed
        assert hundred[0] == Decimal("199.00")         # the record survives
        assert two_hundred[1] is not None              # the delete stage's row

    async def test_dry_run_counts_without_touching(self, slim_seeded):
        settings, now = slim_seeded
        from hd.db import base as db_base

        async with db_base._default.get_session(settings) as session:
            rows, size = await slim_snapshots(session, 30, 180, now=now, dry_run=True)

        assert rows == 1 and size > 0
        assert all(raw is not None for _, raw in await _rows_by_age(settings))

    async def test_slimming_is_idempotent(self, slim_seeded):
        settings, now = slim_seeded
        from hd.db import base as db_base

        async with db_base._default.get_session(settings) as session:
            first = await slim_snapshots(session, 30, 180, now=now)
        async with db_base._default.get_session(settings) as session:
            second = await slim_snapshots(session, 30, 180, now=now)

        assert first[0] == 1
        assert second == (0, 0)

    async def test_disabled_and_inverted_configs_do_nothing(self, slim_seeded):
        settings, now = slim_seeded
        from hd.db import base as db_base

        async with db_base._default.get_session(settings) as session:
            assert await slim_snapshots(session, 0, 90, now=now) == (0, 0)
            assert await slim_snapshots(session, 90, 90, now=now) == (0, 0)
            assert await slim_snapshots(session, 120, 90, now=now) == (0, 0)
        assert all(raw is not None for _, raw in await _rows_by_age(settings))
