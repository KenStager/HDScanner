"""Tests for dashboard formatting helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from hd.dashboard.components.formatters import (
    alert_type_icon,
    fmt_inventory_qty,
    fmt_observed_drop,
    fmt_pct,
    fmt_pct_nonzero,
    fmt_price,
    fmt_savings_center,
    fmt_ts,
    fmt_ts_relative,
    format_alert_details,
    format_price_change,
    product_status_badge,
    severity_color,
    stock_badge,
)


class TestFmtPrice:
    def test_none_returns_dash(self):
        assert fmt_price(None) == "-"

    def test_decimal_value(self):
        assert fmt_price(Decimal("199.99")) == "$199.99"

    def test_large_value_with_commas(self):
        assert fmt_price(Decimal("1299.00")) == "$1,299.00"

    def test_zero(self):
        assert fmt_price(0) == "$0.00"

    def test_float_value(self):
        assert fmt_price(49.99) == "$49.99"


class TestFmtPct:
    def test_none_returns_dash(self):
        assert fmt_pct(None) == "-"

    def test_integer_value(self):
        assert fmt_pct(40) == "40%"

    def test_zero(self):
        assert fmt_pct(0) == "0%"


class TestFmtTs:
    def test_none_returns_dash(self):
        assert fmt_ts(None) == "-"

    def test_datetime_object(self):
        dt = datetime(2025, 6, 15, 14, 30, 45, tzinfo=timezone.utc)
        assert fmt_ts(dt) == "2025-06-15 14:30:45"

    def test_string_value(self):
        assert fmt_ts("2025-06-15 14:30:45.123456") == "2025-06-15 14:30:45"


class TestSeverityColor:
    def test_low(self):
        assert severity_color("low") == "blue"

    def test_medium(self):
        assert severity_color("medium") == "orange"

    def test_high(self):
        assert severity_color("high") == "red"

    def test_unknown(self):
        assert severity_color("critical") == "grey"


class TestAlertTypeIcon:
    def test_price_drop(self):
        assert alert_type_icon("PRICE_DROP") == "trending_down"

    def test_clearance(self):
        assert alert_type_icon("CLEARANCE") == "local_offer"

    def test_oos(self):
        assert alert_type_icon("OOS") == "remove_shopping_cart"

    def test_unknown(self):
        assert alert_type_icon("UNKNOWN") == "info"


class TestStockBadge:
    def test_in_stock(self):
        label, color = stock_badge(True)
        assert label == "In Stock"
        assert color == "green"

    def test_out_of_stock(self):
        label, color = stock_badge(False)
        assert label == "Out of Stock"
        assert color == "red"

    def test_none(self):
        label, color = stock_badge(None)
        assert label == "Unknown"
        assert color == "blue-grey"


class TestFmtPctNonzero:
    def test_none_returns_dash(self):
        assert fmt_pct_nonzero(None) == "-"

    def test_zero_returns_dash(self):
        assert fmt_pct_nonzero(0) == "-"

    def test_zero_float_returns_dash(self):
        assert fmt_pct_nonzero(0.0) == "-"

    def test_positive_value(self):
        assert fmt_pct_nonzero(40) == "40%"

    def test_float_value(self):
        assert fmt_pct_nonzero(12.5) == "12.5%"


class TestFmtSavingsCenter:
    def test_none_returns_dash(self):
        assert fmt_savings_center(None) == "-"

    def test_empty_string_returns_dash(self):
        assert fmt_savings_center("") == "-"

    def test_clearance(self):
        assert fmt_savings_center("CLEARANCE") == "Clearance"

    def test_clearance_lowercase(self):
        assert fmt_savings_center("clearance") == "Clearance"

    def test_special_buy(self):
        assert fmt_savings_center("SPECIAL_BUY") == "Special Buy"

    def test_special_buys_variant(self):
        assert fmt_savings_center("SPECIAL_BUYS") == "Special Buy"

    def test_unknown_value_titlecased(self):
        assert fmt_savings_center("SOME_OTHER_STATUS") == "Some Other Status"


class TestFmtTsRelative:
    def test_none_returns_dash(self):
        assert fmt_ts_relative(None) == "-"

    def test_just_now(self):
        now = datetime.now(timezone.utc)
        result = fmt_ts_relative(now)
        assert result == "just now"

    def test_minutes_ago(self):
        from datetime import timedelta
        ts = datetime.now(timezone.utc) - timedelta(minutes=15)
        result = fmt_ts_relative(ts)
        assert result == "15m ago"

    def test_hours_ago(self):
        from datetime import timedelta
        ts = datetime.now(timezone.utc) - timedelta(hours=3)
        result = fmt_ts_relative(ts)
        assert result == "3h ago"

    def test_days_ago(self):
        from datetime import timedelta
        ts = datetime.now(timezone.utc) - timedelta(days=5)
        result = fmt_ts_relative(ts)
        assert result == "5d ago"

    def test_months_ago(self):
        from datetime import timedelta
        ts = datetime.now(timezone.utc) - timedelta(days=60)
        result = fmt_ts_relative(ts)
        assert result == "2mo ago"

    def test_naive_datetime(self):
        """Naive datetimes are treated as UTC."""
        from datetime import timedelta
        ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        result = fmt_ts_relative(ts)
        assert result == "2h ago"

    def test_iso_string(self):
        """ISO format strings are parsed."""
        from datetime import timedelta
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        result = fmt_ts_relative(ts)
        assert result == "1h ago"


class TestFmtObservedDrop:
    def test_none_current_returns_none(self):
        assert fmt_observed_drop(None, 649.0) is None

    def test_none_baseline_returns_none(self):
        assert fmt_observed_drop(599.0, None) is None

    def test_price_at_baseline_returns_none(self):
        """No discount has occurred if current == baseline."""
        assert fmt_observed_drop(649.0, 649.0) is None

    def test_price_above_baseline_returns_none(self):
        """Price went up — not a discount from our perspective."""
        assert fmt_observed_drop(699.0, 649.0) is None

    def test_zero_baseline_returns_none(self):
        """Guard against division by zero."""
        assert fmt_observed_drop(0.0, 0.0) is None

    def test_real_drop_returns_formatted_string(self):
        """$649 → $599 is approximately 7.7% below baseline."""
        result = fmt_observed_drop(599.0, 649.0)
        assert result is not None
        assert "8% below baseline" in result  # rounds to 8%

    def test_large_drop(self):
        """$1199 structural bundle at $649 with no observed history shows 0 — but
        if we actually DID observe $1199 first and now see $649, that's 46% off."""
        result = fmt_observed_drop(649.0, 1199.0)
        assert result is not None
        assert "46% below baseline" in result

    def test_clearance_drop(self):
        """$149 down from first-observed $199 is 25%."""
        result = fmt_observed_drop(149.0, 199.0)
        assert result is not None
        assert "25% below baseline" in result


class TestProductStatusBadge:
    def test_clearance_wins(self):
        """CLEARANCE takes priority over price drop."""
        result = product_status_badge(
            ["CLEARANCE"], [(149.0, 199.0)]
        )
        assert result == ("CLEARANCE", "red")

    def test_any_store_clearance(self):
        """If any store has CLEARANCE, badge is CLEARANCE."""
        result = product_status_badge(
            [None, "CLEARANCE"], [(199.0, 199.0), (149.0, 199.0)]
        )
        assert result == ("CLEARANCE", "red")

    def test_price_drop_no_clearance(self):
        """No clearance but price dropped — shows orange drop badge."""
        result = product_status_badge(
            [None], [(149.0, 199.0)]
        )
        assert result is not None
        label, color = result
        assert "25% drop" in label
        assert color == "orange"

    def test_picks_largest_drop(self):
        """Multiple stores — uses the largest drop."""
        result = product_status_badge(
            [None, None],
            [(190.0, 200.0), (100.0, 200.0)],  # 5% vs 50%
        )
        assert result is not None
        assert "50% drop" in result[0]

    def test_no_badge_no_changes(self):
        """No clearance, prices at baseline — no badge."""
        result = product_status_badge(
            [None], [(199.0, 199.0)]
        )
        assert result is None

    def test_none_inputs(self):
        """None prices should not crash."""
        result = product_status_badge(
            [None], [(None, None)]
        )
        assert result is None


class TestFmtInventoryQty:
    def test_positive_qty(self):
        assert fmt_inventory_qty(5, True) == "5 units"

    def test_zero_qty_in_stock(self):
        assert fmt_inventory_qty(0, True) == "In Stock"

    def test_none_qty_in_stock(self):
        assert fmt_inventory_qty(None, True) == "In Stock"

    def test_none_qty_oos(self):
        assert fmt_inventory_qty(None, False) == "Out of Stock"

    def test_none_qty_none_stock(self):
        assert fmt_inventory_qty(None, None) == "Unknown"


class TestFormatPriceChange:
    def test_price_drop_with_pct(self):
        payload = {
            "before": {"price_value": 599.00},
            "after": {"price_value": 449.00},
            "pct_drop": 25.0,
        }
        result = format_price_change("PRICE_DROP", payload)
        assert "$599.00" in result
        assert "$449.00" in result
        assert "-25%" in result

    def test_price_drop_without_pct(self):
        payload = {
            "before": {"price_value": 599.00},
            "after": {"price_value": 449.00},
        }
        result = format_price_change("PRICE_DROP", payload)
        assert "$599.00" in result
        assert "$449.00" in result
        assert "%" not in result

    def test_clearance_with_pct_off(self):
        payload = {
            "after": {"price_value": 149.99, "percentage_off": 40},
        }
        result = format_price_change("CLEARANCE", payload)
        assert "$149.99" in result
        assert "40% off" in result

    def test_clearance_without_pct_off(self):
        payload = {"after": {"price_value": 149.99}}
        result = format_price_change("CLEARANCE", payload)
        assert "$149.99" in result
        assert "off" not in result

    def test_oos(self):
        payload = {
            "before": {"in_stock": True},
            "after": {"in_stock": False},
        }
        result = format_price_change("OOS", payload)
        assert "In Stock" in result
        assert "Out of Stock" in result

    def test_back_in_stock(self):
        payload = {
            "before": {"in_stock": False},
            "after": {"in_stock": True},
        }
        result = format_price_change("BACK_IN_STOCK", payload)
        assert "Out of Stock" in result
        assert "In Stock" in result

    def test_special_buy(self):
        payload = {"after": {"price_value": 299.00}}
        result = format_price_change("SPECIAL_BUY", payload)
        assert "$299.00" in result
        assert "Special Buy" in result

    def test_empty_payload(self):
        assert format_price_change("PRICE_DROP", None) == ""
        assert format_price_change("PRICE_DROP", {}) == ""

    def test_fallback_with_title(self):
        payload = {"product_title": "Milwaukee M18 FUEL"}
        result = format_price_change("HEALTH_DEGRADED", payload)
        assert "Milwaukee" in result


class TestFormatAlertDetails:
    def test_price_drop(self):
        payload = {
            "before": {"price_value": "199.99"},
            "after": {"price_value": "149.99"},
        }
        result = format_alert_details("PRICE_DROP", payload)
        assert "$199.99" in result
        assert "$149.99" in result

    def test_clearance(self):
        payload = {"after": {"percentage_off": 40}}
        result = format_alert_details("CLEARANCE", payload)
        assert "40% off" in result

    def test_empty_payload(self):
        assert format_alert_details("PRICE_DROP", None) == ""
        assert format_alert_details("PRICE_DROP", {}) == ""

    def test_other_type_with_title(self):
        payload = {"product_title": "Milwaukee M18 FUEL Hammer Drill"}
        result = format_alert_details("BACK_IN_STOCK", payload)
        assert "Milwaukee" in result
