"""Tests for the diff engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from hd.config import Settings
from hd.db.base import Database
from hd.db.models import StoreSnapshot, Product, AlertType, Severity
from hd.pipeline.diff import _diff_snapshots, run_diff


def _make_snapshot(**kwargs) -> StoreSnapshot:
    """Helper to create a StoreSnapshot with defaults."""
    defaults = {
        "id": 1,
        "ts": datetime.now(timezone.utc),
        "store_id": "2619",
        "item_id": "312345678",
        "price_value": Decimal("249.00"),
        "price_original": Decimal("299.00"),
        "promotion_type": None,
        "promotion_tag": None,
        "savings_center": None,
        "dollar_off": None,
        "percentage_off": None,
        "special_buy": None,
        "inventory_qty": 10,
        "in_stock": True,
        "limited_qty": False,
        "out_of_stock": False,
        "raw_json": None,
    }
    defaults.update(kwargs)
    snap = StoreSnapshot()
    for k, v in defaults.items():
        setattr(snap, k, v)
    return snap


def _make_product() -> Product:
    return Product(
        item_id="312345678",
        brand="Milwaukee",
        title="Milwaukee M18 FUEL Impact Wrench",
        canonical_url="/p/Milwaukee-M18-FUEL/312345678",
        model_number="2767-20",
    )


class TestPriceDrop:
    def test_small_drop_ignored(self):
        """~17% drop is below 25% threshold — no alert."""
        prev = _make_snapshot(price_value=Decimal("299.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("249.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 0

    def test_medium_severity_price_drop(self):
        """30% drop exceeds 25% threshold — MEDIUM."""
        prev = _make_snapshot(price_value=Decimal("200.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("140.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 1
        assert price_alerts[0].severity == Severity.MEDIUM

    def test_high_severity_price_drop(self):
        """55% drop — HIGH."""
        prev = _make_snapshot(price_value=Decimal("200.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("90.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 1
        assert price_alerts[0].severity == Severity.HIGH

    def test_no_alert_price_increase(self):
        prev = _make_snapshot(price_value=Decimal("100.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("150.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 0

    def test_no_alert_same_price(self):
        prev = _make_snapshot(price_value=Decimal("100.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("100.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 0

    def test_handles_null_prices(self):
        prev = _make_snapshot(price_value=None)
        curr = _make_snapshot(id=2, price_value=Decimal("100.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 0

    def test_borderline_25_pct_not_triggered(self):
        """Exactly 25% is not > 25%, so no alert."""
        prev = _make_snapshot(price_value=Decimal("200.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("150.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 0


class TestClearance:
    def test_detects_clearance_transition(self):
        prev = _make_snapshot(savings_center=None, percentage_off=None)
        curr = _make_snapshot(id=2, savings_center="CLEARANCE", percentage_off=40)
        alerts = _diff_snapshots(prev, curr, _make_product())

        cl_alerts = [a for a in alerts if a.alert_type == AlertType.CLEARANCE]
        assert len(cl_alerts) == 1
        assert cl_alerts[0].severity == Severity.MEDIUM

    def test_high_severity_clearance(self):
        prev = _make_snapshot(savings_center=None)
        curr = _make_snapshot(id=2, savings_center="CLEARANCE", percentage_off=55)
        alerts = _diff_snapshots(prev, curr, _make_product())

        cl_alerts = [a for a in alerts if a.alert_type == AlertType.CLEARANCE]
        assert len(cl_alerts) == 1
        assert cl_alerts[0].severity == Severity.HIGH

    def test_no_alert_if_already_clearance(self):
        prev = _make_snapshot(savings_center="CLEARANCE")
        curr = _make_snapshot(id=2, savings_center="CLEARANCE")
        alerts = _diff_snapshots(prev, curr, _make_product())

        cl_alerts = [a for a in alerts if a.alert_type == AlertType.CLEARANCE]
        assert len(cl_alerts) == 0


class TestRemovedAlertTypes:
    """SPECIAL_BUY, BACK_IN_STOCK, and OOS alerts are no longer generated."""

    def test_special_buy_not_generated(self):
        prev = _make_snapshot(special_buy=False)
        curr = _make_snapshot(id=2, special_buy=True)
        alerts = _diff_snapshots(prev, curr, _make_product())
        assert all(a.alert_type not in (
            AlertType.SPECIAL_BUY, AlertType.BACK_IN_STOCK, AlertType.OOS,
        ) for a in alerts)

    def test_stock_changes_not_generated(self):
        prev = _make_snapshot(in_stock=True)
        curr = _make_snapshot(id=2, in_stock=False)
        alerts = _diff_snapshots(prev, curr, _make_product())
        assert all(a.alert_type not in (
            AlertType.BACK_IN_STOCK, AlertType.OOS,
        ) for a in alerts)


class TestNoChange:
    def test_no_alerts_when_nothing_changed(self):
        prev = _make_snapshot()
        curr = _make_snapshot(id=2)
        alerts = _diff_snapshots(prev, curr, _make_product())
        assert len(alerts) == 0

    def test_payload_has_before_after(self):
        prev = _make_snapshot(price_value=Decimal("300.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("200.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())

        assert len(alerts) >= 1
        payload = alerts[0].payload
        assert "before" in payload
        assert "after" in payload
        assert payload["before"]["price_value"] == 300.00
        assert payload["after"]["price_value"] == 200.00
        assert "product_title" in payload


# --- Diff gap awareness tests (use run_diff with in-memory DB) ---

@pytest.fixture
def gap_settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        stores="2619",
        brands="Milwaukee",
        product_line_filters="M12,M18",
        store_raw_json=False,
        diff_gap_threshold_hours=48,
        diff_stale_gap_hours=168,
    )


@pytest_asyncio.fixture
async def seeded_gap_settings(gap_settings: Settings):
    """Initialize DB with product and snapshots at various time gaps."""
    from hd.db import base as db_base

    db_base._default = Database()
    await db_base._default.init_db(gap_settings)
    yield gap_settings
    await db_base._default.close_db()


async def _seed_snapshots_with_gap(settings, gap_hours: float, price_prev=Decimal("299.00"), price_curr=Decimal("199.00")):
    """Helper: seed a product + two snapshots separated by gap_hours."""
    from hd.db import base as db_base

    now = datetime.now(timezone.utc)
    async with db_base._default.get_session(settings) as session:
        session.add(Product(
            item_id="GAP001",
            brand="Milwaukee",
            title="Gap Test Product",
            is_active=True,
            first_seen_ts=now - timedelta(hours=gap_hours + 1),
            last_seen_ts=now,
        ))
        session.add(StoreSnapshot(
            store_id="2619",
            item_id="GAP001",
            ts=now - timedelta(hours=gap_hours),
            price_value=price_prev,
            in_stock=True,
        ))
        session.add(StoreSnapshot(
            store_id="2619",
            item_id="GAP001",
            ts=now,
            price_value=price_curr,
            in_stock=True,
        ))


class TestDiffGapAwareness:
    async def test_stale_gap_skips_diff(self, seeded_gap_settings):
        """Snapshots 10 days apart (>168h stale threshold) should produce 0 alerts."""
        await _seed_snapshots_with_gap(seeded_gap_settings, gap_hours=240)
        alerts = await run_diff(seeded_gap_settings)
        assert len(alerts) == 0

    async def test_moderate_gap_annotates_alerts(self, seeded_gap_settings):
        """Snapshots 72h apart (>48h threshold) should annotate alerts with gap_warning."""
        await _seed_snapshots_with_gap(seeded_gap_settings, gap_hours=72)
        alerts = await run_diff(seeded_gap_settings)

        assert len(alerts) >= 1
        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 1
        assert price_alerts[0].payload.get("gap_warning") is True
        assert price_alerts[0].payload.get("gap_hours") == 72.0

    async def test_normal_gap_no_annotation(self, seeded_gap_settings):
        """Snapshots 2 hours apart should NOT have gap_warning in payload."""
        await _seed_snapshots_with_gap(seeded_gap_settings, gap_hours=2)
        alerts = await run_diff(seeded_gap_settings)

        assert len(alerts) >= 1
        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 1
        assert "gap_warning" not in price_alerts[0].payload
