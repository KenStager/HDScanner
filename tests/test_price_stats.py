"""Tests for item_price_stats — the history that must survive pruning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from hd.config import Settings
from hd.db.base import Database
from hd.db.models import ItemPriceStat, StoreSnapshot
from hd.db.price_stats import apply_observation, backfill, mean_price, record_observations


@pytest.fixture
def stats_settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        stores="2619,8425",
        store_raw_json=False,
    )


@pytest_asyncio.fixture
async def stats_db(stats_settings: Settings) -> Settings:
    from hd.db import base as db_base

    db_base._default = Database()
    await db_base._default.init_db(stats_settings)
    return stats_settings


class TestApplyObservation:
    """The pure fold — one definition shared by the write path and backfill."""

    def test_first_observation_seeds_every_field(self):
        s = ItemPriceStat(store_id="2619", item_id="1")
        t = datetime(2026, 3, 9, 12, 0)
        apply_observation(s, Decimal("29.97"), t)
        assert s.low_price == s.high_price == Decimal("29.97")
        assert s.low_ts == s.high_ts == t
        assert s.obs_count == 1 and s.obs_days == 1
        assert mean_price(s) == 29.97

    def test_tracks_low_and_high_with_their_dates(self):
        s = ItemPriceStat(store_id="2619", item_id="1")
        base = datetime(2026, 3, 9, 12, 0)
        for i, p in enumerate(["99.00", "42.93", "120.00", "60.00"]):
            apply_observation(s, Decimal(p), base + timedelta(days=i))
        assert s.low_price == Decimal("42.93")
        assert s.low_ts == base + timedelta(days=1)
        assert s.high_price == Decimal("120.00")
        assert s.high_ts == base + timedelta(days=2)

    def test_same_day_scans_count_as_one_day(self):
        """Six scans in an afternoon are one day of evidence, not six."""
        s = ItemPriceStat(store_id="2619", item_id="1")
        day = datetime(2026, 3, 9, 4, 0)
        for h in range(0, 24, 4):
            apply_observation(s, Decimal("50.00"), day.replace(hour=h))
        assert s.obs_count == 6
        assert s.obs_days == 1

    def test_mean_is_exact_across_observations(self):
        s = ItemPriceStat(store_id="2619", item_id="1")
        base = datetime(2026, 3, 9, 12, 0)
        for i, p in enumerate(["100.00", "200.00", "300.00"]):
            apply_observation(s, Decimal(p), base + timedelta(days=i))
        assert mean_price(s) == 200.0

    def test_unpriced_observation_is_ignored(self):
        s = ItemPriceStat(store_id="2619", item_id="1")
        apply_observation(s, None, datetime(2026, 3, 9))
        assert s.obs_count in (0, None)
        assert mean_price(s) is None

    def test_naive_and_aware_timestamps_interoperate(self):
        """The pipeline passes aware UTC; the DB returns naive. Both must fold."""
        s = ItemPriceStat(store_id="2619", item_id="1")
        apply_observation(s, Decimal("10.00"), datetime(2026, 3, 9, 12, 0))
        apply_observation(
            s, Decimal("8.00"), datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
        )
        assert s.obs_days == 2
        assert s.low_price == Decimal("8.00")


class TestRecordObservations:
    async def test_creates_then_accumulates(self, stats_db: Settings):
        from hd.db import base as db_base

        base = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)
        async with db_base._default.get_session(stats_db) as session:
            await record_observations(session, "2619", [("100001", Decimal("99.00"), base)])
        async with db_base._default.get_session(stats_db) as session:
            await record_observations(
                session, "2619", [("100001", Decimal("42.93"), base + timedelta(days=1))]
            )
            stat = await session.get(ItemPriceStat, ("2619", "100001"))
            assert stat.obs_count == 2
            assert stat.obs_days == 2
            assert stat.low_price == Decimal("42.93")

    async def test_stores_are_tracked_separately(self, stats_db: Settings):
        from hd.db import base as db_base

        t = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)
        async with db_base._default.get_session(stats_db) as session:
            await record_observations(session, "2619", [("100001", Decimal("99.00"), t)])
            await record_observations(session, "8425", [("100001", Decimal("49.00"), t)])
        async with db_base._default.get_session(stats_db) as session:
            a = await session.get(ItemPriceStat, ("2619", "100001"))
            b = await session.get(ItemPriceStat, ("8425", "100001"))
            assert a.low_price == Decimal("99.00")
            assert b.low_price == Decimal("49.00")


class TestBackfill:
    async def test_reconstructs_history_from_raw_snapshots(self, stats_db: Settings):
        from hd.db import base as db_base

        base = datetime(2026, 3, 9, 12, 0)
        prices = ["99.00", "42.93", "120.00", "60.00"]
        async with db_base._default.get_session(stats_db) as session:
            for i, p in enumerate(prices):
                session.add(StoreSnapshot(
                    store_id="2619", item_id="100001",
                    ts=base + timedelta(days=i), price_value=Decimal(p),
                ))
            session.add(StoreSnapshot(   # unpriced rows must not skew the mean
                store_id="2619", item_id="100001",
                ts=base + timedelta(days=4), price_value=None,
            ))

        scanned, items = await backfill(stats_db)
        assert (scanned, items) == (4, 1)

        async with db_base._default.get_session(stats_db) as session:
            stat = await session.get(ItemPriceStat, ("2619", "100001"))
            assert stat.low_price == Decimal("42.93")
            assert stat.high_price == Decimal("120.00")
            assert stat.obs_count == 4 and stat.obs_days == 4
            assert mean_price(stat) == pytest.approx(80.4825)

    async def test_backfill_matches_the_incremental_path(self, stats_db: Settings):
        """Both callers share one fold, so both must land on identical numbers."""
        from hd.db import base as db_base

        base = datetime(2026, 3, 9, 12, 0)
        prices = [Decimal(p) for p in ["50.00", "35.00", "80.00", "35.00", "44.00"]]
        async with db_base._default.get_session(stats_db) as session:
            for i, p in enumerate(prices):
                session.add(StoreSnapshot(
                    store_id="2619", item_id="100001",
                    ts=base + timedelta(days=i), price_value=p,
                ))
            await record_observations(
                session, "2619",
                [("100002", p, base + timedelta(days=i)) for i, p in enumerate(prices)],
            )
            incremental = await session.get(ItemPriceStat, ("2619", "100002"))
            expected = (incremental.low_price, incremental.high_price,
                        incremental.obs_count, incremental.obs_days, incremental.price_sum)

        await backfill(stats_db)
        async with db_base._default.get_session(stats_db) as session:
            rebuilt = await session.get(ItemPriceStat, ("2619", "100001"))
            assert (rebuilt.low_price, rebuilt.high_price, rebuilt.obs_count,
                    rebuilt.obs_days, rebuilt.price_sum) == expected

    async def test_backfill_replaces_rather_than_doubling(self, stats_db: Settings):
        """Running it twice must not inflate obs_count — it rebuilds, not merges."""
        from hd.db import base as db_base

        base = datetime(2026, 3, 9, 12, 0)
        async with db_base._default.get_session(stats_db) as session:
            for i in range(3):
                session.add(StoreSnapshot(
                    store_id="2619", item_id="100001",
                    ts=base + timedelta(days=i), price_value=Decimal("10.00"),
                ))
        await backfill(stats_db)
        await backfill(stats_db)
        async with db_base._default.get_session(stats_db) as session:
            stat = await session.get(ItemPriceStat, ("2619", "100001"))
            assert stat.obs_count == 3


class TestWritePathWiring:
    """The pipeline's only snapshot writer must maintain the aggregate too."""

    async def test_insert_snapshots_updates_stats(self, stats_db: Settings):
        from hd.db import base as db_base
        from hd.hd_api.models import NormalizedSnapshot
        from hd.pipeline.snapshot import _insert_snapshots

        now = datetime.now(timezone.utc)
        snaps = [
            NormalizedSnapshot(item_id="100001", store_id="2619", price_value=99.00),
            NormalizedSnapshot(item_id="100002", store_id="2619", price_value=None),
        ]
        inserted = await _insert_snapshots(stats_db, snaps, "2619", now)
        assert inserted == 2

        async with db_base._default.get_session(stats_db) as session:
            priced = await session.get(ItemPriceStat, ("2619", "100001"))
            assert priced.obs_count == 1
            assert priced.low_price == Decimal("99.00")
            # an unpriced observation records no price facts at all
            assert await session.get(ItemPriceStat, ("2619", "100002")) is None

    async def test_second_run_accumulates_not_overwrites(self, stats_db: Settings):
        from hd.db import base as db_base
        from hd.hd_api.models import NormalizedSnapshot
        from hd.pipeline.snapshot import _insert_snapshots

        day1 = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)
        day2 = day1 + timedelta(days=1)
        await _insert_snapshots(
            stats_db, [NormalizedSnapshot(item_id="1", store_id="2619", price_value=80.0)],
            "2619", day1)
        await _insert_snapshots(
            stats_db, [NormalizedSnapshot(item_id="1", store_id="2619", price_value=60.0)],
            "2619", day2)

        async with db_base._default.get_session(stats_db) as session:
            stat = await session.get(ItemPriceStat, ("2619", "1"))
            assert stat.obs_count == 2 and stat.obs_days == 2
            assert stat.low_price == Decimal("60.00")
            assert stat.high_price == Decimal("80.00")


class TestObservationCarriesItsRegion:
    """An observation must remember which walk produced it.

    Coverage records name a REGION; price rows name an ITEM. Nothing joined
    them, and the absence rule needs exactly that join — an item may be called
    absent only from a walk recorded complete over that item's region. The
    mapping is knowable only in flight, so a walk that does not record it
    destroys it for good.
    """

    async def test_a_walked_observation_records_its_node(self, stats_db: Settings):
        from sqlalchemy import select

        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot
        from hd.hd_api.models import NormalizedSnapshot
        from hd.pipeline.snapshot import _insert_snapshots

        now = datetime.now(timezone.utc)
        nav = "N-5yc1vZzvZc1xyZc298Zc28l"  # Milwaukee/Tools/Power Tools/Saws
        await _insert_snapshots(
            stats_db, [NormalizedSnapshot(item_id="1", store_id="2619", price_value=99.0)],
            "2619", now, nav)

        async with db_base._default.get_session(stats_db) as session:
            stored = (await session.execute(
                select(StoreSnapshot.nav_param))).scalars().all()
        assert stored == [nav]

    async def test_an_unwalked_observation_records_no_region_not_a_wrong_one(
        self, stats_db: Settings
    ):
        """The daily-deals sweep and the keyword path walk no node. NULL means
        "region unknown" and must never read as a match for any region."""
        from sqlalchemy import select

        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot
        from hd.hd_api.models import NormalizedSnapshot
        from hd.pipeline.snapshot import _insert_snapshots

        now = datetime.now(timezone.utc)
        await _insert_snapshots(
            stats_db, [NormalizedSnapshot(item_id="1", store_id="2619", price_value=99.0)],
            "2619", now)

        async with db_base._default.get_session(stats_db) as session:
            stored = (await session.execute(
                select(StoreSnapshot.nav_param))).scalars().all()
        assert stored == [None]

    async def test_the_region_joins_an_observation_to_its_coverage_record(
        self, stats_db: Settings
    ):
        """Given an item, find the walk that covers it, and ask whether that
        walk completed.

        ⚠️ The join below is deliberately minimal because this install scans
        ONE store. It is NOT a predicate to copy: nav_param is composed from
        the catalog root plus facet tokens, so the store is not encoded in it
        and one value names the same region at EVERY store. Anything reasoning
        from an item's absence must also qualify by store, by tier (a shelf
        walk is not evidence about the online catalogue), and by run (this
        table is append-only, so "completed once, ever" is not "completed in
        the cycle being judged").
        """
        from sqlalchemy import select

        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot, WalkCoverage
        from hd.hd_api.models import NormalizedSnapshot
        from hd.pipeline.snapshot import _insert_snapshots

        now = datetime.now(timezone.utc)
        nav = "N-5yc1vZzvZc1xyZc298Zc28l"
        await _insert_snapshots(
            stats_db, [NormalizedSnapshot(item_id="1", store_id="2619", price_value=99.0)],
            "2619", now, nav)
        async with db_base._default.get_session(stats_db) as session:
            session.add(WalkCoverage(
                run_id=1, store_id="2619", tier="ALL", label="M/Tools/Saws",
                nav_param=nav, started=now, ended=now, status="truncated",
                items_expected=295, items_observed=71,
            ))
            await session.commit()

        async with db_base._default.get_session(stats_db) as session:
            status = (await session.execute(
                select(WalkCoverage.status)
                .join(StoreSnapshot, StoreSnapshot.nav_param == WalkCoverage.nav_param)
                .where(StoreSnapshot.item_id == "1"))).scalars().all()
        # Item 1's region was walked, and that walk did NOT complete — so
        # nothing may reason from this item's absence.
        assert status == ["truncated"]
