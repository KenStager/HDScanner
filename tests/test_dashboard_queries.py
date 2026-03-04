"""Tests for dashboard query functions using in-memory SQLite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hd.config import Settings
from hd.db.base import Database
from hd.db.models import (
    Alert,
    AlertType,
    Base,
    Product,
    Severity,
    Store,
    StoreSnapshot,
)
from hd.dashboard.queries import (
    get_alerts,
    get_overview_stats,
    get_product_detail,
    get_products_with_latest,
    get_store_summary,
)


@pytest.fixture
def dashboard_settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        stores="2619,8425",
        brands="Milwaukee",
        product_line_filters="M12,M18",
        store_raw_json=False,
    )


@pytest_asyncio.fixture
async def seeded_settings(dashboard_settings: Settings) -> Settings:
    """Initialize DB with seed data and return settings.

    Uses the module-level default Database instance so queries.py
    (which calls get_session via the same default instance) works.
    """
    from hd.db import base as db_base

    # Reset the default instance to use our test settings
    db_base._default = Database()
    await db_base._default.init_db(dashboard_settings)

    now = datetime.now(timezone.utc)
    async with db_base._default.get_session(dashboard_settings) as session:
        # Stores
        session.add(Store(store_id="2619", name="Store A"))
        session.add(Store(store_id="8425", name="Store B"))

        # Products
        session.add(Product(
            item_id="100001",
            brand="Milwaukee",
            title="M18 FUEL Hammer Drill",
            model_number="2904-20",
            is_active=True,
            first_seen_ts=now - timedelta(days=30),
            last_seen_ts=now,
        ))
        session.add(Product(
            item_id="100002",
            brand="Milwaukee",
            title="M12 Impact Driver",
            model_number="2553-20",
            is_active=True,
            first_seen_ts=now - timedelta(days=15),
            last_seen_ts=now,
        ))
        session.add(Product(
            item_id="100003",
            brand="DeWalt",
            title="20V MAX Drill",
            model_number="DCD771",
            is_active=False,  # inactive
            first_seen_ts=now - timedelta(days=60),
            last_seen_ts=now - timedelta(days=30),
        ))

        # Snapshots — store 2619
        # Product 100001: two snapshots (older + newer)
        session.add(StoreSnapshot(
            store_id="2619", item_id="100001",
            ts=now - timedelta(hours=48),
            price_value=Decimal("199.00"),
            in_stock=True, out_of_stock=False,
        ))
        session.add(StoreSnapshot(
            store_id="2619", item_id="100001",
            ts=now - timedelta(hours=2),
            price_value=Decimal("149.00"),
            savings_center="CLEARANCE",
            percentage_off=25,
            in_stock=True, out_of_stock=False,
        ))

        # Product 100002: one snapshot — OOS
        session.add(StoreSnapshot(
            store_id="2619", item_id="100002",
            ts=now - timedelta(hours=3),
            price_value=Decimal("99.00"),
            in_stock=False, out_of_stock=True,
        ))

        # Snapshots — store 8425
        session.add(StoreSnapshot(
            store_id="8425", item_id="100001",
            ts=now - timedelta(hours=1),
            price_value=Decimal("199.00"),
            in_stock=True, out_of_stock=False,
        ))

        # Alerts
        session.add(Alert(
            store_id="2619", item_id="100001",
            alert_type=AlertType.PRICE_DROP,
            severity=Severity.HIGH,
            ts=now - timedelta(hours=2),
            payload={"before": {"price_value": "199.00"}, "after": {"price_value": "149.00"}},
        ))
        session.add(Alert(
            store_id="2619", item_id="100001",
            alert_type=AlertType.CLEARANCE,
            severity=Severity.MEDIUM,
            ts=now - timedelta(hours=2),
            payload={"after": {"percentage_off": 25}},
        ))
        session.add(Alert(
            store_id="2619", item_id="100002",
            alert_type=AlertType.OOS,
            severity=Severity.LOW,
            ts=now - timedelta(hours=3),
            payload={"product_title": "M12 Impact Driver"},
        ))
        # Old alert — outside 24h window
        session.add(Alert(
            store_id="8425", item_id="100001",
            alert_type=AlertType.PRICE_DROP,
            severity=Severity.MEDIUM,
            ts=now - timedelta(hours=30),
            payload={},
        ))

    yield dashboard_settings

    await db_base._default.close_db()


class TestOverviewStats:
    async def test_active_products_count(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["active_products"] == 2  # 100001 + 100002 active, 100003 inactive

    async def test_total_snapshots(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["total_snapshots"] == 4  # 3 for 2619 + 1 for 8425

    async def test_alert_count_24h(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["alert_count_24h"] == 3  # 3 within 24h, 1 outside

    async def test_clearance_detection(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["clearance_count"] == 1  # only 100001@2619 latest is CLEARANCE

    async def test_oos_count(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["oos_count"] == 1  # only 100002@2619 is OOS

    async def test_health_healthy(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["health_status"] == "HEALTHY"

    async def test_latest_snapshot_ts_present(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["latest_snapshot_ts"] is not None

    async def test_price_drops_7d(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        # 2 PRICE_DROP alerts for item_id 100001 (stores 2619 + 8425) → distinct count = 1
        assert stats["price_drops_7d"] == 1


class TestProductsWithLatest:
    async def test_returns_active_products(self, seeded_settings: Settings):
        rows = await get_products_with_latest(seeded_settings, ["2619", "8425"])
        item_ids = [r["item_id"] for r in rows]
        assert "100001" in item_ids
        assert "100002" in item_ids
        assert "100003" not in item_ids  # inactive

    async def test_latest_price_per_store(self, seeded_settings: Settings):
        rows = await get_products_with_latest(seeded_settings, ["2619", "8425"])
        p1 = next(r for r in rows if r["item_id"] == "100001")
        # Latest 2619 snapshot: $149
        assert p1["price_2619"] == 149.00
        # Latest 8425 snapshot: $199
        assert p1["price_8425"] == 199.00

    async def test_stock_status(self, seeded_settings: Settings):
        rows = await get_products_with_latest(seeded_settings, ["2619"])
        p2 = next(r for r in rows if r["item_id"] == "100002")
        assert p2["in_stock_2619"] is False


class TestProductDetail:
    async def test_snapshots_ordered_asc(self, seeded_settings: Settings):
        detail = await get_product_detail(seeded_settings, "100001")
        snapshots = detail["snapshots"]
        assert len(snapshots) >= 2
        # Verify chronological order
        timestamps = [s["ts"] for s in snapshots]
        assert timestamps == sorted(timestamps)

    async def test_includes_alerts(self, seeded_settings: Settings):
        detail = await get_product_detail(seeded_settings, "100001")
        assert len(detail["alerts"]) == 3  # PRICE_DROP@2619 + CLEARANCE@2619 + PRICE_DROP@8425

    async def test_product_info(self, seeded_settings: Settings):
        detail = await get_product_detail(seeded_settings, "100001")
        assert detail["product"]["brand"] == "Milwaukee"
        assert detail["product"]["model_number"] == "2904-20"

    async def test_nonexistent_product(self, seeded_settings: Settings):
        detail = await get_product_detail(seeded_settings, "999999")
        assert detail["product"] is None
        assert detail["snapshots"] == []
        assert detail["alerts"] == []


class TestAlerts:
    async def test_filter_by_type(self, seeded_settings: Settings):
        rows = await get_alerts(seeded_settings, alert_type="PRICE_DROP")
        assert all(r["alert_type"] == "PRICE_DROP" for r in rows)
        assert len(rows) == 2  # one within 24h, one outside

    async def test_filter_by_severity(self, seeded_settings: Settings):
        rows = await get_alerts(seeded_settings, severity="high")
        assert all(r["severity"] == "high" for r in rows)
        assert len(rows) == 1

    async def test_filter_by_store(self, seeded_settings: Settings):
        rows = await get_alerts(seeded_settings, store_id="8425")
        assert all(r["store_id"] == "8425" for r in rows)

    async def test_filter_by_since(self, seeded_settings: Settings):
        rows = await get_alerts(seeded_settings, since_hours=24)
        assert len(rows) == 3  # 3 within 24h

    async def test_includes_product_title(self, seeded_settings: Settings):
        rows = await get_alerts(seeded_settings, limit=10)
        titled = [r for r in rows if r["product_title"]]
        assert len(titled) > 0

    async def test_limit(self, seeded_settings: Settings):
        rows = await get_alerts(seeded_settings, limit=2)
        assert len(rows) == 2


class TestStoreSummary:
    async def test_returns_all_stores(self, seeded_settings: Settings):
        summaries = await get_store_summary(seeded_settings)
        store_ids = [s["store_id"] for s in summaries]
        assert "2619" in store_ids
        assert "8425" in store_ids

    async def test_store_aggregates(self, seeded_settings: Settings):
        summaries = await get_store_summary(seeded_settings)
        s2619 = next(s for s in summaries if s["store_id"] == "2619")
        assert s2619["total_products"] == 2
        assert s2619["clearance"] == 1
        assert s2619["oos"] == 1

    async def test_price_drops_7d(self, seeded_settings: Settings):
        """price_drops_7d counts distinct items with PRICE_DROP alerts in last 7d."""
        summaries = await get_store_summary(seeded_settings)
        s2619 = next(s for s in summaries if s["store_id"] == "2619")
        # Seeded data has one PRICE_DROP alert for item 100001 at store 2619 within 2h
        assert s2619["price_drops_7d"] == 1

    async def test_no_avg_discount_pct(self, seeded_settings: Settings):
        """avg_discount_pct has been removed — it was averaging structural bundle
        offsets (API percentage_off) which are not indicators of real price drops."""
        summaries = await get_store_summary(seeded_settings)
        for s in summaries:
            assert "avg_discount_pct" not in s

    async def test_price_drops_7d_zero_for_store_with_no_drops(self, seeded_settings: Settings):
        """Store 8425 has no PRICE_DROP alert within 7 days in the seeded data
        (its PRICE_DROP alert is 30 hours old, which IS within 7 days — so it counts)."""
        summaries = await get_store_summary(seeded_settings)
        s8425 = next(s for s in summaries if s["store_id"] == "8425")
        # The seeded alert for 8425 is 30h old, within the 7d window
        assert s8425["price_drops_7d"] == 1
