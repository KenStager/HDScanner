"""Tests for the diff engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from hd.config import Settings
from hd.db.base import Database
from hd.db.models import Alert, StoreSnapshot, Product, AlertType, Severity
from hd.pipeline.diff import _diff_snapshots, _is_combo_kit, _cold_start_check, _product_url, run_diff


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
        "clearance_value": None,
        "clearance_dollar_off": None,
        "clearance_percentage_off": None,
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


def _make_product(**kwargs) -> Product:
    defaults = {
        "item_id": "312345678",
        "brand": "Milwaukee",
        "title": "Milwaukee M18 FUEL Impact Wrench",
        "canonical_url": "/p/Milwaukee-M18-FUEL/312345678",
        "model_number": "2767-20",
    }
    defaults.update(kwargs)
    return Product(**defaults)


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
        prev = _make_snapshot(price_value=Decimal("200.00"), price_original=None)
        curr = _make_snapshot(id=2, price_value=Decimal("150.00"), price_original=None)
        alerts = _diff_snapshots(prev, curr, _make_product())

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 0

    def test_cumulative_drop_fires_when_step_is_small(self):
        """Small step-down but cumulative from baseline >=35% fires PRICE_DROP."""
        # Baseline was 200, prev was 140 (already dropped), curr is 125 (another small step)
        # Single step: (140-125)/140 = 10.7% — below 25%
        # Cumulative: (200-125)/200 = 37.5% — above 35%
        prev = _make_snapshot(price_value=Decimal("140.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("125.00"))
        baseline = Decimal("200.00")
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=baseline)

        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP]
        assert len(price_alerts) == 1
        assert price_alerts[0].payload.get("cumulative_drop") is True

    def test_cumulative_drop_not_fired_when_below_threshold(self):
        """Cumulative below 35% does not fire even with a step down."""
        prev = _make_snapshot(price_value=Decimal("170.00"), price_original=None)
        curr = _make_snapshot(id=2, price_value=Decimal("140.00"), price_original=None)
        baseline = Decimal("200.00")
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=baseline)

        # step: 17.6%, cumulative: 30% — both below thresholds
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
        """Clearance with >50% observed drop from reference fires HIGH."""
        prev = _make_snapshot(
            savings_center=None,
            price_value=Decimal("299.00"),
            price_original=Decimal("299.00"),
        )
        curr = _make_snapshot(
            id=2,
            savings_center="CLEARANCE",
            percentage_off=55,
            price_value=Decimal("120.00"),
            price_original=Decimal("299.00"),
        )
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

    def test_clearance_escalates_when_deep_off_original(self):
        """Clearance with price < 60% of price_original escalates to HIGH."""
        prev = _make_snapshot(savings_center=None, price_value=Decimal("299.00"))
        curr = _make_snapshot(
            id=2,
            savings_center="CLEARANCE",
            price_value=Decimal("100.00"),
            price_original=Decimal("299.00"),
            percentage_off=66,
        )
        baseline = Decimal("299.00")
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=baseline)

        cl_alerts = [a for a in alerts if a.alert_type == AlertType.CLEARANCE]
        assert len(cl_alerts) == 1
        assert cl_alerts[0].severity == Severity.HIGH


class TestComboKit:
    def test_combo_kit_by_model_number(self):
        product = _make_product(model_number="2997-22")
        assert _is_combo_kit(product) is True

    def test_single_tool_not_combo(self):
        product = _make_product(model_number="2767-20")
        assert _is_combo_kit(product) is False

    def test_combo_kit_by_title(self):
        product = _make_product(title="Milwaukee M18 4-Tool Combo Kit", model_number="X-00")
        assert _is_combo_kit(product) is True

    def test_combo_kit_by_tool_paren(self):
        product = _make_product(title="Milwaukee M18 (2-Tool)", model_number="X-00")
        assert _is_combo_kit(product) is True

    def test_none_product(self):
        assert _is_combo_kit(None) is False

    def test_clearance_not_escalated_for_combo(self):
        """Combo kits should NOT use price_original for escalation."""
        combo = _make_product(model_number="2997-22", title="Milwaukee Combo Kit")
        prev = _make_snapshot(savings_center=None, price_value=Decimal("599.00"))
        curr = _make_snapshot(
            id=2,
            savings_center="CLEARANCE",
            price_value=Decimal("300.00"),
            price_original=Decimal("800.00"),  # inflated sum-of-parts
            percentage_off=50,
        )
        baseline = Decimal("599.00")
        alerts = _diff_snapshots(prev, curr, combo, baseline_price=baseline)

        cl_alerts = [a for a in alerts if a.alert_type == AlertType.CLEARANCE]
        assert len(cl_alerts) == 1
        # Should be HIGH from effective_pct (50%) but NOT from price_original escalation
        assert cl_alerts[0].severity == Severity.HIGH


class TestPricingError:
    def test_pricing_error_extreme_discount_no_promo(self):
        """>=75% off with no promo metadata fires PRICING_ERROR."""
        prev = _make_snapshot(price_value=Decimal("299.00"))
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("49.00"),
            savings_center=None,
            promotion_tag=None,
        )
        baseline = Decimal("299.00")
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=baseline)

        pe_alerts = [a for a in alerts if a.alert_type == AlertType.PRICING_ERROR]
        assert len(pe_alerts) >= 1
        assert pe_alerts[0].severity == Severity.HIGH
        assert pe_alerts[0].payload["detection_reason"] == "extreme_discount_no_promo"

    def test_no_pricing_error_with_promo(self):
        """>=75% off WITH promo metadata does NOT fire PRICING_ERROR."""
        prev = _make_snapshot(price_value=Decimal("299.00"))
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("49.00"),
            savings_center="CLEARANCE",
            promotion_tag="Clearance",
        )
        baseline = Decimal("299.00")
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=baseline)

        pe_alerts = [a for a in alerts if a.alert_type == AlertType.PRICING_ERROR]
        assert len(pe_alerts) == 0

    def test_no_pricing_error_below_threshold(self):
        """50% off without promo does NOT fire (below 75% threshold)."""
        prev = _make_snapshot(price_value=Decimal("299.00"))
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("150.00"),
            savings_center=None,
            promotion_tag=None,
        )
        baseline = Decimal("299.00")
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=baseline)

        pe_alerts = [a for a in alerts if a.alert_type == AlertType.PRICING_ERROR]
        assert len(pe_alerts) == 0

    def test_single_step_crash_fires(self):
        """>=60% single-step drop without promo fires PRICING_ERROR."""
        prev = _make_snapshot(price_value=Decimal("299.00"))
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("100.00"),  # 66.6% step drop
            savings_center=None,
            promotion_tag=None,
        )
        baseline = Decimal("299.00")
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=baseline)

        pe_alerts = [a for a in alerts if a.alert_type == AlertType.PRICING_ERROR]
        # Should have step crash detection (pct_off_ref=66.6% < 75%, but step_drop=66.6% >= 60%)
        step_crash = [a for a in pe_alerts if a.payload.get("detection_reason") == "single_step_crash_no_promo"]
        assert len(step_crash) == 1


class TestColdStart:
    def test_cold_start_clearance(self):
        """First snapshot with >=40% off original + savings_center fires alert."""
        curr = _make_snapshot(
            price_value=Decimal("149.00"),
            price_original=Decimal("299.00"),
            savings_center="CLEARANCE",
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            cold_start_clearance_pct=40,
        )
        alerts = _cold_start_check(curr, _make_product(), settings)

        assert len(alerts) >= 1
        assert alerts[0].payload.get("cold_start") is True

    def test_cold_start_no_alert_below_threshold(self):
        """First snapshot with only 20% off does not fire."""
        curr = _make_snapshot(
            price_value=Decimal("239.00"),
            price_original=Decimal("299.00"),
            savings_center="CLEARANCE",
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            cold_start_clearance_pct=40,
        )
        alerts = _cold_start_check(curr, _make_product(), settings)
        assert len(alerts) == 0

    def test_cold_start_combo_kit_ignored(self):
        """Combo kits should not trigger cold-start alerts."""
        combo = _make_product(model_number="2997-22", title="Milwaukee Combo Kit")
        curr = _make_snapshot(
            price_value=Decimal("399.00"),
            price_original=Decimal("800.00"),
            savings_center="CLEARANCE",
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            cold_start_clearance_pct=40,
        )
        alerts = _cold_start_check(curr, combo, settings)
        assert len(alerts) == 0

    def test_cold_start_no_savings_center_no_clearance_alert(self):
        """No savings_center means no cold-start clearance (but pricing error possible)."""
        curr = _make_snapshot(
            price_value=Decimal("50.00"),
            price_original=Decimal("299.00"),
            savings_center=None,
            promotion_tag=None,
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            cold_start_clearance_pct=40,
            pricing_error_threshold_pct=75,
        )
        alerts = _cold_start_check(curr, _make_product(), settings)

        # Should get pricing error (83% off, no promo) but NOT clearance
        clearance = [a for a in alerts if a.alert_type == AlertType.CLEARANCE]
        pe = [a for a in alerts if a.alert_type == AlertType.PRICING_ERROR]
        assert len(clearance) == 0
        assert len(pe) == 1


class TestSpecialBuyFallback:
    def test_special_buy_fallback_to_original(self):
        """Special Buy with no baseline and unstable price_original uses fallback."""
        # price_original differs between snapshots — ref can't use it
        prev = _make_snapshot(
            savings_center=None,
            price_value=Decimal("100.00"),
            price_original=Decimal("350.00"),
        )
        curr = _make_snapshot(
            id=2,
            savings_center="Special Buys",
            price_value=Decimal("100.00"),
            price_original=Decimal("299.00"),
        )
        # No baseline, unstable price_original → ref is None → fallback path
        alerts = _diff_snapshots(prev, curr, _make_product(), baseline_price=None)

        sb_alerts = [a for a in alerts if a.alert_type == AlertType.SPECIAL_BUY]
        assert len(sb_alerts) == 1
        assert sb_alerts[0].payload.get("fallback_to_original") is True

    def test_special_buy_no_fallback_for_combo(self):
        """Combo kit should not use price_original fallback for Special Buy."""
        combo = _make_product(model_number="2997-22", title="Milwaukee Combo Kit")
        prev = _make_snapshot(
            savings_center=None,
            price_value=Decimal("400.00"),
            price_original=Decimal("900.00"),  # unstable → ref stays None
        )
        curr = _make_snapshot(
            id=2,
            savings_center="Special Buys",
            price_value=Decimal("400.00"),
            price_original=Decimal("800.00"),
        )
        alerts = _diff_snapshots(prev, curr, combo, baseline_price=None)

        sb_alerts = [a for a in alerts if a.alert_type == AlertType.SPECIAL_BUY]
        assert len(sb_alerts) == 0


class TestRemovedAlertTypes:
    """BACK_IN_STOCK and OOS alerts are no longer generated."""

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


class TestInStoreClearance:
    """Tests for IN_STORE_CLEARANCE alert detection."""

    def test_clearance_appeared(self):
        """Clearance value appearing should fire IN_STORE_CLEARANCE."""
        prev = _make_snapshot(
            price_value=Decimal("299.00"),
            clearance_value=None,
        )
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("299.00"),
            clearance_value=Decimal("150.00"),
            clearance_percentage_off=50,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 1
        assert cl_alerts[0].severity == Severity.HIGH
        assert cl_alerts[0].payload["clearance_value"] == 150.0
        assert cl_alerts[0].payload["clearance_percentage_off"] == 50
        assert cl_alerts[0].payload["online_price"] == 299.0

    def test_clearance_deepened(self):
        """Clearance price dropping further should fire IN_STORE_CLEARANCE."""
        prev = _make_snapshot(
            price_value=Decimal("299.00"),
            clearance_value=Decimal("200.00"),
            clearance_percentage_off=33,
        )
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("299.00"),
            clearance_value=Decimal("150.00"),
            clearance_percentage_off=50,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 1
        assert cl_alerts[0].payload["prev_clearance_value"] == 200.0

    def test_clearance_stable_no_alert(self):
        """Unchanged clearance value should NOT fire alert."""
        prev = _make_snapshot(
            price_value=Decimal("299.00"),
            clearance_value=Decimal("150.00"),
            clearance_percentage_off=50,
        )
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("299.00"),
            clearance_value=Decimal("150.00"),
            clearance_percentage_off=50,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 0

    def test_no_clearance_no_alert(self):
        """No clearance data should not fire IN_STORE_CLEARANCE."""
        prev = _make_snapshot(price_value=Decimal("299.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("299.00"))
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 0

    def test_oos_locally_and_not_purchasable_online_suppressed(self):
        """OOS locally + clearance price only in-store → no alert (unactionable)."""
        prev = _make_snapshot(clearance_value=None)
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("299.00"),      # online still full price
            clearance_value=Decimal("150.00"),
            in_stock=False,
            out_of_stock=True,
            inventory_qty=None,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 0

    def test_oos_locally_but_online_price_at_clearance_alerts(self):
        """OOS locally but online price matches the clearance price → purchasable online, alert."""
        prev = _make_snapshot(clearance_value=None)
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("150.00"),      # online price reflects clearance
            clearance_value=Decimal("150.00"),
            in_stock=False,
            out_of_stock=True,
            inventory_qty=None,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 1

    def test_oos_but_partial_online_discount_still_suppressed(self):
        """Online discounted, but not to the clearance price → clearance still unobtainable, no alert."""
        prev = _make_snapshot(clearance_value=None)
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("79.00"),       # online discount exists...
            price_original=Decimal("119.00"),
            clearance_value=Decimal("22.00"),   # ...but the $22 clearance is in-store only
            in_stock=False,
            out_of_stock=True,
            inventory_qty=None,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 0

    def test_in_stock_locally_alerts_regardless_of_online_price(self):
        """Shelf stock present → alert even though clearance is in-store only."""
        prev = _make_snapshot(clearance_value=None)
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("299.00"),
            clearance_value=Decimal("150.00"),
            in_stock=True,
            inventory_qty=1,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 1

    def test_no_inventory_signal_suppressed_unless_online_reflects(self):
        """No inventory data at all → suppressed when online price is still full."""
        prev = _make_snapshot(clearance_value=None)
        curr = _make_snapshot(
            id=2,
            price_value=Decimal("299.00"),
            clearance_value=Decimal("150.00"),
            in_stock=None,
            out_of_stock=None,
            inventory_qty=None,
        )
        alerts = _diff_snapshots(prev, curr, _make_product())
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 0


class TestColdStartClearance:
    """Tests for cold-start IN_STORE_CLEARANCE detection."""

    def test_cold_start_with_clearance(self):
        """First snapshot with clearance should fire IN_STORE_CLEARANCE."""
        curr = _make_snapshot(
            price_value=Decimal("299.00"),
            clearance_value=Decimal("150.00"),
            clearance_percentage_off=50,
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            cold_start_clearance_pct=40,
            pricing_error_threshold_pct=75,
        )
        alerts = _cold_start_check(curr, _make_product(), settings)
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 1
        assert cl_alerts[0].payload["clearance_value"] == 150.0
        assert cl_alerts[0].payload["cold_start"] is True

    def test_cold_start_without_clearance(self):
        """First snapshot without clearance should NOT fire IN_STORE_CLEARANCE."""
        curr = _make_snapshot(
            price_value=Decimal("249.00"),
            price_original=Decimal("299.00"),
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            cold_start_clearance_pct=40,
            pricing_error_threshold_pct=75,
        )
        alerts = _cold_start_check(curr, _make_product(), settings)
        cl_alerts = [a for a in alerts if a.alert_type == AlertType.IN_STORE_CLEARANCE]
        assert len(cl_alerts) == 0


class TestDuplicateSnapshotDedup:
    """Tests that duplicate snapshots (same timestamp) are handled correctly."""

    async def test_duplicate_snapshots_dont_mask_diff(self, seeded_gap_settings):
        """Items with duplicate rows per run should still detect price changes."""
        from hd.db import base as db_base

        now = datetime.now(timezone.utc)
        prev_ts = now - timedelta(hours=4)

        async with db_base._default.get_session(seeded_gap_settings) as session:
            session.add(Product(
                item_id="DUP001",
                brand="Milwaukee",
                title="Dup Test Product",
                is_active=True,
                first_seen_ts=prev_ts,
                last_seen_ts=now,
            ))
            # Previous run: one snapshot at prev_ts
            session.add(StoreSnapshot(
                store_id="2619",
                item_id="DUP001",
                ts=prev_ts,
                price_value=Decimal("299.00"),
                in_stock=True,
            ))
            # Current run: TWO identical snapshots at now (the bug)
            for _ in range(2):
                session.add(StoreSnapshot(
                    store_id="2619",
                    item_id="DUP001",
                    ts=now,
                    price_value=Decimal("199.00"),
                    in_stock=True,
                ))

        alerts = await run_diff(seeded_gap_settings)
        price_alerts = [a for a in alerts if a.alert_type == AlertType.PRICE_DROP and a.item_id == "DUP001"]
        assert len(price_alerts) == 1, "Duplicate snapshots should not mask price changes"


class TestProductUrl:
    """Tests for _product_url() fallback helper."""

    def test_canonical_url_present(self):
        product = _make_product(canonical_url="/p/Milwaukee-M18-FUEL/312345678")
        url = _product_url(product, "312345678")
        assert url == "https://www.homedepot.com/p/Milwaukee-M18-FUEL/312345678"

    def test_canonical_url_none(self):
        product = _make_product(canonical_url=None)
        url = _product_url(product, "312345678")
        assert url == "https://www.homedepot.com/s/312345678"

    def test_product_none(self):
        url = _product_url(None, "312345678")
        assert url == "https://www.homedepot.com/s/312345678"

    def test_canonical_url_empty_string(self):
        product = _make_product(canonical_url="")
        url = _product_url(product, "312345678")
        assert url == "https://www.homedepot.com/s/312345678"

    def test_diff_payload_always_has_product_url(self):
        """_diff_snapshots payloads always have a non-None product_url."""
        prev = _make_snapshot(price_value=Decimal("200.00"))
        curr = _make_snapshot(id=2, price_value=Decimal("140.00"))
        product = _make_product(canonical_url=None)
        alerts = _diff_snapshots(prev, curr, product)
        for alert in alerts:
            assert alert.payload.get("product_url") is not None
            assert "homedepot.com" in alert.payload["product_url"]


class TestDismissedAlertSuppression:
    def test_dismissed_at_same_price_suppressed(self):
        from hd.pipeline.diff import _drop_dismissed

        alert = Alert(
            store_id="2619", item_id="303229042",
            alert_type=AlertType.IN_STORE_CLEARANCE, severity=Severity.HIGH,
            ts=datetime.now(timezone.utc),
            payload={"clearance_value": 175.0},
        )
        kept = _drop_dismissed([alert], {("2619", "303229042"): 175.0})
        assert kept == []

    def test_deeper_price_alerts_again(self):
        from hd.pipeline.diff import _drop_dismissed

        alert = Alert(
            store_id="2619", item_id="303229042",
            alert_type=AlertType.IN_STORE_CLEARANCE, severity=Severity.HIGH,
            ts=datetime.now(timezone.utc),
            payload={"clearance_value": 120.0},
        )
        kept = _drop_dismissed([alert], {("2619", "303229042"): 175.0})
        assert kept == [alert]

    def test_other_alert_types_untouched(self):
        from hd.pipeline.diff import _drop_dismissed

        alert = Alert(
            store_id="2619", item_id="303229042",
            alert_type=AlertType.PRICE_DROP, severity=Severity.MEDIUM,
            ts=datetime.now(timezone.utc),
            payload={},
        )
        kept = _drop_dismissed([alert], {("2619", "303229042"): 175.0})
        assert kept == [alert]
