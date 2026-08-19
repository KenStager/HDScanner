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
    ItemPriceStat,
    Product,
    Severity,
    Store,
    StoreSnapshot,
)
from hd.dashboard.queries import (
    get_alerts,
    get_deal_board,
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
            clearance_value=Decimal("99.00"),
            clearance_percentage_off=50,
            in_stock=True, out_of_stock=False, inventory_qty=3,
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

        # Durable price stats — one configured store, one retired store id
        # that must never leak onto the product page
        session.add(ItemPriceStat(
            store_id="2619", item_id="100001",
            low_price=Decimal("149.00"), low_ts=now - timedelta(hours=2),
            high_price=Decimal("199.00"), high_ts=now - timedelta(days=20),
            price_sum=Decimal("348.00"), obs_count=2, obs_days=2,
            first_ts=now - timedelta(days=20), last_ts=now - timedelta(hours=2),
        ))
        session.add(ItemPriceStat(
            store_id="9999", item_id="100001",
            low_price=Decimal("1.00"), low_ts=now,
            high_price=Decimal("1.00"), high_ts=now,
            price_sum=Decimal("1.00"), obs_count=1, obs_days=1,
            first_ts=now, last_ts=now,
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
        # only 100001@2619 latest carries an actionable clearance_value
        assert stats["clearance_count"] == 1

    async def test_oos_count(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["oos_count"] == 1  # only 100002@2619 is OOS

    async def test_health_healthy(self, seeded_settings: Settings):
        stats = await get_overview_stats(seeded_settings)
        assert stats["health_status"] == "OK"

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
        assert detail["price_stats"] == {}

    async def test_snapshots_carry_clearance_fields(self, seeded_settings: Settings):
        """The page leads with the price you'd pay — clearance must be there."""
        detail = await get_product_detail(seeded_settings, "100001")
        latest_2619 = [s for s in detail["snapshots"] if s["store_id"] == "2619"][-1]
        assert latest_2619["clearance_value"] == 99.00
        assert latest_2619["clearance_percentage_off"] == 50

    async def test_price_stats_per_store(self, seeded_settings: Settings):
        detail = await get_product_detail(seeded_settings, "100001")
        stats = detail["price_stats"]["2619"]
        assert stats["low_price"] == 149.00
        assert stats["high_price"] == 199.00
        assert stats["obs_days"] == 2
        assert stats["low_ts"] is not None
        assert stats["first_ts"] is not None

    async def test_price_stats_exclude_unconfigured_store(self, seeded_settings: Settings):
        """Retired store ids in item_price_stats must not leak onto the page."""
        detail = await get_product_detail(seeded_settings, "100001")
        assert "9999" not in detail["price_stats"]


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


class TestDealBoard:
    async def test_actionable_clearance_appears_with_details(self, seeded_settings: Settings):
        board = await get_deal_board(seeded_settings)
        deals = board["stores"].get("2619", [])
        assert [d["item_id"] for d in deals] == ["100001"]
        deal = deals[0]
        assert deal["clearance_value"] == 99.0
        assert deal["pct_off"] == 50
        assert deal["qty"] == 3
        assert deal["url"].startswith("https://www.homedepot.com")
        assert deal["is_new"] is True  # first clearance snapshot is 2h old

    async def test_unpurchasable_clearance_excluded(self, seeded_settings: Settings):
        """OOS + online price above clearance → not on the board."""
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="8425", item_id="100002",
                ts=datetime.now(timezone.utc) - timedelta(minutes=5),
                price_value=Decimal("99.00"),
                clearance_value=Decimal("40.00"),
                in_stock=False, out_of_stock=True, inventory_qty=None,
            ))
        board = await get_deal_board(seeded_settings)
        assert "100002" not in [d["item_id"] for d in board["stores"].get("8425", [])]

    async def test_store_names_returned(self, seeded_settings: Settings):
        board = await get_deal_board(seeded_settings)
        assert isinstance(board["store_names"], dict)


class TestDismissals:
    async def test_dismissed_deal_hidden_from_board(self, seeded_settings: Settings):
        from hd.dashboard.queries import dismiss_deal

        await dismiss_deal(seeded_settings, "2619", "100001", 99.0)
        board = await get_deal_board(seeded_settings)
        assert board["stores"]["2619"] == []
        assert [d["item_id"] for d in board["hidden"]["2619"]] == ["100001"]

    async def test_deeper_deal_resurfaces(self, seeded_settings: Settings):
        from datetime import datetime, timezone
        from decimal import Decimal
        from hd.dashboard.queries import dismiss_deal
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        await dismiss_deal(seeded_settings, "2619", "100001", 99.0)
        # Clearance drops below the dismissed price → new situation
        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100001",
                ts=datetime.now(timezone.utc),
                price_value=Decimal("149.00"),
                clearance_value=Decimal("75.00"),
                clearance_percentage_off=62,
                in_stock=True, inventory_qty=2,
            ))
        board = await get_deal_board(seeded_settings)
        assert [d["item_id"] for d in board["stores"]["2619"]] == ["100001"]

    async def test_restore_deal(self, seeded_settings: Settings):
        from hd.dashboard.queries import dismiss_deal, restore_deal

        await dismiss_deal(seeded_settings, "2619", "100001", 99.0)
        await restore_deal(seeded_settings, "2619", "100001")
        board = await get_deal_board(seeded_settings)
        assert [d["item_id"] for d in board["stores"]["2619"]] == ["100001"]

    async def test_zero_deal_store_still_listed(self, seeded_settings: Settings):
        board = await get_deal_board(seeded_settings)
        assert "8425" in board["stores"]
        assert board["stores"]["8425"] == []


class TestOnlineDeals:
    async def test_special_buy_with_history_shows_true_savings(self, seeded_settings: Settings):
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        now = datetime.now(timezone.utc)
        async with db_base._default.get_session(seeded_settings) as session:
            # history: $199 ten days ago, now special buy at $99 claiming 50% off $199
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=now - timedelta(days=10),
                price_value=Decimal("199.00"), in_stock=True,
            ))
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=now,
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, special_buy=True,
                in_stock=True,
            ))
        deals = await get_online_deals(seeded_settings)
        deal = next(d for d in deals if d["item_id"] == "100002")
        assert deal["claimed_pct"] == 50
        assert deal["true_pct"] == 50  # 199 -> 99 in our own history
        assert deal["special_buy"] is True

    async def test_inflated_was_price_shows_flat_history(self, seeded_settings: Settings):
        """Price claims 50% off but our 30d history says it never changed."""
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        now = datetime.now(timezone.utc)
        async with db_base._default.get_session(seeded_settings) as session:
            for days_ago in (20, 10):
                session.add(StoreSnapshot(
                    store_id="2619", item_id="100002",
                    ts=now - timedelta(days=days_ago),
                    price_value=Decimal("99.00"), in_stock=True,
                ))
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=now,
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50,
                in_stock=True,
            ))
        deals = await get_online_deals(seeded_settings)
        deal = next(d for d in deals if d["item_id"] == "100002")
        assert deal["claimed_pct"] == 50
        assert deal["true_pct"] == 0  # $99 for 20+ days — the discount is fiction

    async def test_online_dismissal_flag(self, seeded_settings: Settings):
        from datetime import datetime, timezone
        from decimal import Decimal
        from hd.dashboard.queries import ONLINE_STORE_KEY, dismiss_deal, get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=datetime.now(timezone.utc),
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, in_stock=True,
            ))
        await dismiss_deal(seeded_settings, ONLINE_STORE_KEY, "100002", 99.0)
        deals = await get_online_deals(seeded_settings)
        deal = next(d for d in deals if d["item_id"] == "100002")
        assert deal["dismissed"] is True


class TestOnlineDealsAvailability:
    async def test_confirmed_oos_deal_excluded(self, seeded_settings: Settings):
        from datetime import datetime, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        oos_raw = {"fulfillment": {"fulfillmentOptions": [{
            "type": "delivery",
            "services": [{"type": "sth", "locations": [
                {"locationId": "x", "inventory": {"isInStock": False, "isOutOfStock": True}},
            ]}],
        }]}}
        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=datetime.now(timezone.utc),
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, in_stock=False,
                raw_json=oos_raw,
            ))
        deals = await get_online_deals(seeded_settings)
        assert "100002" not in [d["item_id"] for d in deals]

    async def test_unknown_fulfillment_kept(self, seeded_settings: Settings):
        """No raw fulfillment data — keep the deal rather than false-negative it."""
        from datetime import datetime, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=datetime.now(timezone.utc),
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, in_stock=False,
                raw_json=None,
            ))
        deals = await get_online_deals(seeded_settings)
        assert "100002" in [d["item_id"] for d in deals]


class TestDealFreshness:
    async def test_stale_online_deal_excluded(self, seeded_settings: Settings):
        """An item unseen for 10 days is out of the catalog — not a deal."""
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=datetime.now(timezone.utc) - timedelta(days=10),
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, in_stock=True,
            ))
        deals = await get_online_deals(seeded_settings)
        assert "100002" not in [d["item_id"] for d in deals]

    async def test_stale_clearance_excluded_from_board(self, seeded_settings: Settings):
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="8425", item_id="100002",
                ts=datetime.now(timezone.utc) - timedelta(days=10),
                price_value=Decimal("199.00"),
                clearance_value=Decimal("50.00"),
                in_stock=True, inventory_qty=5,
            ))
        board = await get_deal_board(seeded_settings)
        assert board["stores"]["8425"] == []


class TestOnlineDealsHistoryDepth:
    async def test_fresh_item_gets_no_history_verdict(self, seeded_settings: Settings):
        """An item first seen today must not be labeled 'flat price'."""
        from datetime import datetime, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=datetime.now(timezone.utc),
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, special_buy=True, in_stock=True,
            ))
        deals = await get_online_deals(seeded_settings)
        deal = next(d for d in deals if d["item_id"] == "100002")
        assert deal["high_window"] is None   # no verdict — history too shallow
        assert deal["history_days"] is None
        assert deal["true_pct"] == 0
        assert deal["claimed_pct"] == 50

    async def test_verdict_reports_the_span_it_observed(self, seeded_settings: Settings):
        """A verdict must carry the age of the history behind it, not a fixed window."""
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        now = datetime.now(timezone.utc)
        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=now - timedelta(days=20),
                price_value=Decimal("199.00"), in_stock=True,
            ))
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002", ts=now,
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, special_buy=True, in_stock=True,
            ))
        deals = await get_online_deals(seeded_settings)
        deal = next(d for d in deals if d["item_id"] == "100002")
        assert deal["history_days"] == 20
        assert deal["high_window"] == 199.00
        assert deal["true_pct"] == 50

    async def test_history_predating_the_window_is_not_counted(self, seeded_settings: Settings):
        """Snapshots older than the window are pruned territory — never claim them."""
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        now = datetime.now(timezone.utc)
        window = seeded_settings.deal_history_window_days
        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(   # far outside the window
                store_id="2619", item_id="100002",
                ts=now - timedelta(days=window + 60),
                price_value=Decimal("999.00"), in_stock=True,
            ))
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002",
                ts=now - timedelta(days=10),
                price_value=Decimal("199.00"), in_stock=True,
            ))
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002", ts=now,
                price_value=Decimal("99.00"),
                price_original=Decimal("199.00"),
                percentage_off=50, special_buy=True, in_stock=True,
            ))
        deals = await get_online_deals(seeded_settings)
        deal = next(d for d in deals if d["item_id"] == "100002")
        assert deal["history_days"] == 10       # not window + 60
        assert deal["high_window"] == 199.00    # the $999 outlier is out of scope


class TestOnlineDealPriceAnchor:
    """The witnessed-low anchor: durable, dated, and suppressed when it says nothing."""

    async def _seed(self, settings, *, low, high, low_days_ago, price):
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from hd.db import base as db_base
        from hd.db.models import ItemPriceStat, StoreSnapshot

        now = datetime.now(timezone.utc)
        async with db_base._default.get_session(settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002", ts=now,
                price_value=Decimal(str(price)), price_original=Decimal("199.00"),
                percentage_off=50, special_buy=True, in_stock=True,
            ))
            session.add(ItemPriceStat(
                store_id="2619", item_id="100002",
                low_price=Decimal(str(low)), low_ts=now - timedelta(days=low_days_ago),
                high_price=Decimal(str(high)), high_ts=now - timedelta(days=30),
                price_sum=Decimal("500.00"), obs_count=5, obs_days=5,
                first_ts=now - timedelta(days=40), last_ts=now,
            ))

    async def test_exposes_a_witnessed_lower_price(self, seeded_settings: Settings):
        from hd.dashboard.queries import get_online_deals

        await self._seed(seeded_settings, low=42.93, high=99.00, low_days_ago=100, price=49.97)
        deal = next(d for d in await get_online_deals(seeded_settings)
                    if d["item_id"] == "100002")
        assert deal["low_price"] == 42.93
        assert deal["price_varied"] is True
        assert deal["low_is_older"] is True

    async def test_flat_price_item_reports_no_variation(self, seeded_settings: Settings):
        """low == high means the price never moved; the anchor must stay silent."""
        from hd.dashboard.queries import get_online_deals

        await self._seed(seeded_settings, low=49.97, high=49.97, low_days_ago=100, price=49.97)
        deal = next(d for d in await get_online_deals(seeded_settings)
                    if d["item_id"] == "100002")
        assert deal["price_varied"] is False

    async def test_low_set_today_is_not_treated_as_history(self, seeded_settings: Settings):
        """An item seen once is at its low by definition — not a fact worth showing."""
        from hd.dashboard.queries import get_online_deals

        await self._seed(seeded_settings, low=42.93, high=99.00, low_days_ago=0, price=42.93)
        deal = next(d for d in await get_online_deals(seeded_settings)
                    if d["item_id"] == "100002")
        assert deal["low_is_older"] is False

    async def test_missing_stats_leave_the_anchor_empty(self, seeded_settings: Settings):
        """Items with no aggregate row must not break the card."""
        from datetime import datetime, timezone
        from decimal import Decimal
        from hd.dashboard.queries import get_online_deals
        from hd.db import base as db_base
        from hd.db.models import StoreSnapshot

        async with db_base._default.get_session(seeded_settings) as session:
            session.add(StoreSnapshot(
                store_id="2619", item_id="100002", ts=datetime.now(timezone.utc),
                price_value=Decimal("99.00"), price_original=Decimal("199.00"),
                percentage_off=50, special_buy=True, in_stock=True,
            ))
        deal = next(d for d in await get_online_deals(seeded_settings)
                    if d["item_id"] == "100002")
        assert deal["low_price"] is None
        assert deal["price_varied"] is False


class TestDealTier:
    """Evidence classifies the card; the grid leads with what we can vouch for."""

    def _deal(self, **kw):
        d = {"price": 100.0, "claimed_pct": 40, "true_pct": 0,
             "evidence_pct": 0, "witnessed_pct": 0, "obs_days": None,
             "low_price": None, "low_ts": None, "low_is_older": False,
             "price_varied": False}
        d.update(kw)
        return d

    def test_claim_only_card_is_unverified(self):
        from hd.dashboard.queries import deal_tier
        assert deal_tier(self._deal()) == "unverified"

    def test_measured_depth_makes_it_verified(self):
        from hd.dashboard.queries import deal_tier
        d = self._deal(evidence_pct=40, witnessed_pct=40,
                       low_price=100.0, low_is_older=True, price_varied=True)
        assert deal_tier(d) == "verified"

    def test_above_recorded_low_is_warned(self):
        from hd.dashboard.queries import deal_tier
        d = self._deal(price=120.0, low_price=100.0, low_is_older=True,
                       price_varied=True, evidence_pct=15)
        assert deal_tier(d) == "warned"

    def test_warning_outranks_a_verified_discount(self):
        """"We watched it sell for less" is decisive, even beside a real drop."""
        from hd.dashboard.queries import deal_tier
        d = self._deal(price=120.0, true_pct=30, evidence_pct=30,
                       low_price=100.0, low_is_older=True, price_varied=True)
        assert deal_tier(d) == "warned"

    def test_long_watched_flat_claim_is_hollow(self):
        """A 'was' price that never existed in 10+ watched days is disproven."""
        from hd.dashboard.queries import deal_tier
        d = self._deal(price_varied=False, obs_days=12)
        assert deal_tier(d) == "hollow"

    def test_briefly_watched_flat_claim_stays_on_the_board(self):
        """The record is young — a 3-day flat watch proves nothing yet."""
        from hd.dashboard.queries import deal_tier
        d = self._deal(price_varied=False, obs_days=3)
        assert deal_tier(d) == "unverified"


class TestStorePageUrl:
    """Home Depot store pages — the only way to point a browser at a store."""

    def _store(self, **kw):
        from hd.db.models import Store
        base = dict(store_id="8452", name="Hadley", state="MA",
                    zip="01035", city="Hadley")
        base.update(kw)
        return Store(**base)

    def test_builds_the_verified_format(self):
        from hd.dashboard.queries import store_page_url
        assert store_page_url(self._store()) == (
            "https://www.homedepot.com/l/Hadley/MA/Hadley/01035/8452"
        )

    def test_city_falls_back_to_store_name(self):
        from hd.dashboard.queries import store_page_url
        assert store_page_url(self._store(city=None)) == (
            "https://www.homedepot.com/l/Hadley/MA/Hadley/01035/8452"
        )

    def test_city_differing_from_name_is_respected(self):
        """A store named for a neighbourhood still sits in its city."""
        from hd.dashboard.queries import store_page_url
        url = store_page_url(self._store(name="N. Cambridge", city="Cambridge"))
        assert url == (
            "https://www.homedepot.com/l/N.-Cambridge/MA/Cambridge/01035/8452"
        )

    def test_missing_details_yield_no_link(self):
        """The stale 8425 row has no location — it must not produce a bad URL."""
        from hd.dashboard.queries import store_page_url
        assert store_page_url(self._store(zip=None)) is None
        assert store_page_url(self._store(name=None, city=None)) is None
        assert store_page_url(self._store(state=None)) is None
