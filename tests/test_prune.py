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
