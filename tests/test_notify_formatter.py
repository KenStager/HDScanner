"""Tests for Slack notification formatter."""

from __future__ import annotations

from datetime import datetime, timezone

from hd.notifiers.formatter import (
    format_slack_blocks,
    format_slack_message,
    _prices_vary,
    _store_price_line,
)
from hd.dashboard.components.formatters import infer_in_stock


def _make_group(
    *,
    alert_type: str = "PRICE_DROP",
    severity: str = "high",
    item_id: str = "315442497",
    product_title: str = "Milwaukee M18 FUEL Drill",
    store_ids_display: str = "2619",
    store_count: int = 1,
    pct_drop: float | None = 17.0,
    price_before: float = 299.0,
    price_after: float = 249.0,
    in_stock: bool = True,
    inventory_qty: int | None = 3,
    product_url: str = "https://homedepot.com/p/315442497",
    store_alerts: list[dict] | None = None,
) -> dict:
    payload = {
        "pct_drop": pct_drop,
        "before": {"price_value": price_before, "in_stock": in_stock},
        "after": {
            "price_value": price_after,
            "in_stock": in_stock,
            "inventory_qty": inventory_qty,
        },
        "product_url": product_url,
    }
    if store_alerts is None:
        store_alerts = [
            {
                "store_id": "2619",
                "payload": payload,
            }
        ]
    return {
        "group_key": f"{item_id}_{alert_type}_1",
        "store_count": store_count,
        "store_ids_display": store_ids_display,
        "ts": datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc),
        "ts_dt": datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc),
        "item_id": item_id,
        "alert_type": alert_type,
        "severity": severity,
        "payload": payload,
        "product_title": product_title,
        "store_alerts": store_alerts,
    }


class TestFormatSlackMessage:
    def test_format_single_price_drop(self):
        groups = [_make_group()]
        msg = format_slack_message(groups)
        assert "*PRICE_DROP*" in msg
        assert "Milwaukee M18 FUEL Drill" in msg
        assert "$299.00" in msg
        assert "$249.00" in msg
        assert "-17%" in msg
        assert "Stores: 2619" in msg
        assert "View on HomeDepot.com" in msg

    def test_format_multi_store_group(self):
        store_alerts = [
            {
                "store_id": "2619",
                "payload": {
                    "after": {"in_stock": True, "inventory_qty": 3},
                },
            },
            {
                "store_id": "8425",
                "payload": {
                    "after": {"in_stock": True, "inventory_qty": 1},
                },
            },
        ]
        groups = [_make_group(
            store_ids_display="2619, 8425",
            store_count=2,
            store_alerts=store_alerts,
        )]
        msg = format_slack_message(groups)
        assert "Stores: 2619, 8425" in msg
        assert "3 units" in msg
        assert "1 unit" in msg
        # Should not say "1 units"
        assert "1 units" not in msg

    def test_format_clearance_alert(self):
        groups = [_make_group(
            alert_type="CLEARANCE",
            pct_drop=None,
            price_before=299.0,
            price_after=199.0,
            store_alerts=[{
                "store_id": "2619",
                "payload": {
                    "after": {
                        "price_value": 199.0,
                        "percentage_off": 33,
                        "in_stock": True,
                        "inventory_qty": 2,
                    },
                },
            }],
        )]
        # Need to set payload after field too
        groups[0]["payload"]["after"]["percentage_off"] = 33
        msg = format_slack_message(groups)
        assert "*CLEARANCE*" in msg
        assert "$199.00" in msg
        assert "33% off" in msg

    def test_format_empty_list(self):
        msg = format_slack_message([])
        assert "No new alerts" in msg

    def test_format_multiple_groups(self):
        groups = [
            _make_group(item_id="111", product_title="Product A"),
            _make_group(item_id="222", product_title="Product B", alert_type="CLEARANCE"),
        ]
        msg = format_slack_message(groups)
        assert "Product A" in msg
        assert "Product B" in msg
        assert "*2 new alerts*" in msg

    def test_header_count(self):
        groups = [_make_group()]
        msg = format_slack_message(groups)
        assert "*1 new alert*" in msg
        # Singular — no trailing 's'
        assert "*1 new alerts*" not in msg

    def test_format_oos_alert(self):
        groups = [_make_group(
            alert_type="OOS",
            store_alerts=[{
                "store_id": "2619",
                "payload": {
                    "before": {"in_stock": True},
                    "after": {"in_stock": False},
                },
            }],
        )]
        groups[0]["payload"]["before"] = {"in_stock": True}
        groups[0]["payload"]["after"] = {"in_stock": False}
        msg = format_slack_message(groups)
        assert "*OOS*" in msg
        assert "In Stock" in msg
        assert "Out of Stock" in msg

    def test_format_back_in_stock(self):
        groups = [_make_group(
            alert_type="BACK_IN_STOCK",
            store_alerts=[{
                "store_id": "2619",
                "payload": {
                    "before": {"in_stock": False},
                    "after": {"in_stock": True, "inventory_qty": 5},
                },
            }],
        )]
        groups[0]["payload"]["before"] = {"in_stock": False}
        groups[0]["payload"]["after"] = {"in_stock": True}
        msg = format_slack_message(groups)
        assert "*BACK_IN_STOCK*" in msg

    def test_format_special_buy(self):
        groups = [_make_group(
            alert_type="SPECIAL_BUY",
            store_alerts=[{
                "store_id": "2619",
                "payload": {
                    "after": {"price_value": 179.0, "in_stock": True},
                },
            }],
        )]
        groups[0]["payload"]["after"] = {"price_value": 179.0}
        msg = format_slack_message(groups)
        assert "*SPECIAL_BUY*" in msg
        assert "Special Buy at $179.00" in msg


class TestPerStorePricing:
    """Tests for per-store price display when prices differ across stores."""

    def test_price_drop_differing_prices_text(self):
        """Multi-store PRICE_DROP with different prices → 'By Store' layout."""
        store_alerts = [
            {
                "store_id": "2619",
                "payload": {
                    "pct_drop": 50.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 149.0, "in_stock": True, "inventory_qty": 3},
                },
            },
            {
                "store_id": "8425",
                "payload": {
                    "pct_drop": 17.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 249.0, "in_stock": True, "inventory_qty": 1},
                },
            },
        ]
        groups = [_make_group(
            store_ids_display="2619, 8425",
            store_count=2,
            store_alerts=store_alerts,
        )]
        msg = format_slack_message(groups)
        assert "By Store:" in msg
        assert "Store 2619: $299.00" in msg
        assert "Store 8425: $299.00" in msg
        assert "$149.00 (-50%)" in msg
        assert "$249.00 (-17%)" in msg
        # Should NOT have a separate "Stock:" line
        assert "Stock:" not in msg

    def test_price_drop_identical_prices_text(self):
        """Multi-store PRICE_DROP with same prices → original layout."""
        store_alerts = [
            {
                "store_id": "2619",
                "payload": {
                    "pct_drop": 17.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 249.0, "in_stock": True, "inventory_qty": 3},
                },
            },
            {
                "store_id": "8425",
                "payload": {
                    "pct_drop": 17.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 249.0, "in_stock": True, "inventory_qty": 1},
                },
            },
        ]
        groups = [_make_group(
            store_ids_display="2619, 8425",
            store_count=2,
            store_alerts=store_alerts,
        )]
        msg = format_slack_message(groups)
        assert "By Store:" not in msg
        assert "$299.00" in msg
        assert "$249.00" in msg
        assert "Stock:" in msg

    def test_clearance_differing_prices_text(self):
        """Multi-store CLEARANCE with different percentages → 'By Store' layout."""
        store_alerts = [
            {
                "store_id": "2619",
                "payload": {
                    "percentage_off": 75,
                    "after": {"price_value": 74.75, "percentage_off": 75, "in_stock": True, "inventory_qty": 2},
                },
            },
            {
                "store_id": "8425",
                "payload": {
                    "percentage_off": 30,
                    "after": {"price_value": 209.00, "percentage_off": 30, "in_stock": True, "inventory_qty": 5},
                },
            },
        ]
        groups = [_make_group(
            alert_type="CLEARANCE",
            pct_drop=None,
            store_ids_display="2619, 8425",
            store_count=2,
            store_alerts=store_alerts,
        )]
        msg = format_slack_message(groups)
        assert "By Store:" in msg
        assert "Store 2619: $74.75 (75% off)" in msg
        assert "Store 8425: $209.00 (30% off)" in msg

    def test_oos_multi_store_never_by_store(self):
        """OOS multi-store alerts never trigger 'By Store' layout."""
        store_alerts = [
            {
                "store_id": "2619",
                "payload": {
                    "before": {"in_stock": True},
                    "after": {"in_stock": False, "inventory_qty": 0},
                },
            },
            {
                "store_id": "8425",
                "payload": {
                    "before": {"in_stock": True},
                    "after": {"in_stock": False, "inventory_qty": 0},
                },
            },
        ]
        groups = [_make_group(
            alert_type="OOS",
            store_ids_display="2619, 8425",
            store_count=2,
            store_alerts=store_alerts,
        )]
        groups[0]["payload"]["before"] = {"in_stock": True}
        groups[0]["payload"]["after"] = {"in_stock": False}
        msg = format_slack_message(groups)
        assert "By Store:" not in msg
        assert "Stock:" in msg

    def test_price_drop_differing_prices_blocks(self):
        """Block Kit path: multi-store with different prices → 'By Store' field."""
        store_alerts = [
            {
                "store_id": "2619",
                "payload": {
                    "pct_drop": 50.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 149.0, "in_stock": True, "inventory_qty": 3},
                },
            },
            {
                "store_id": "8425",
                "payload": {
                    "pct_drop": 17.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 249.0, "in_stock": True, "inventory_qty": 1},
                },
            },
        ]
        groups = [_make_group(
            store_ids_display="2619, 8425",
            store_count=2,
            store_alerts=store_alerts,
        )]
        blocks, _ = format_slack_blocks(groups)
        # Find the fields section
        fields_block = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        assert fields_block
        field_texts = [f["text"] for f in fields_block[0]["fields"]]
        by_store = [t for t in field_texts if "*By Store:*" in t]
        assert len(by_store) == 1
        assert "Store 2619" in by_store[0]
        assert "Store 8425" in by_store[0]
        # No separate Price or Stock fields
        price_fields = [t for t in field_texts if t.startswith("*Price:*")]
        stock_fields = [t for t in field_texts if t.startswith("*Stock:*")]
        assert len(price_fields) == 0
        assert len(stock_fields) == 0

    def test_price_drop_identical_prices_blocks(self):
        """Block Kit path: identical prices → original Price + Stock fields."""
        store_alerts = [
            {
                "store_id": "2619",
                "payload": {
                    "pct_drop": 17.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 249.0, "in_stock": True, "inventory_qty": 3},
                },
            },
            {
                "store_id": "8425",
                "payload": {
                    "pct_drop": 17.0,
                    "before": {"price_value": 299.0, "in_stock": True},
                    "after": {"price_value": 249.0, "in_stock": True, "inventory_qty": 1},
                },
            },
        ]
        groups = [_make_group(
            store_ids_display="2619, 8425",
            store_count=2,
            store_alerts=store_alerts,
        )]
        blocks, _ = format_slack_blocks(groups)
        fields_block = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        assert fields_block
        field_texts = [f["text"] for f in fields_block[0]["fields"]]
        by_store = [t for t in field_texts if "*By Store:*" in t]
        assert len(by_store) == 0
        price_fields = [t for t in field_texts if t.startswith("*Price:*")]
        stock_fields = [t for t in field_texts if t.startswith("*Stock:*")]
        assert len(price_fields) == 1
        assert len(stock_fields) == 1

    def test_single_store_unchanged(self):
        """Single store → no 'By Store' layout in either path."""
        groups = [_make_group()]
        msg = format_slack_message(groups)
        assert "By Store:" not in msg
        assert "$299.00" in msg
        blocks, _ = format_slack_blocks(groups)
        fields_block = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        field_texts = [f["text"] for f in fields_block[0]["fields"]]
        assert not any("*By Store:*" in t for t in field_texts)


class TestStorePriceLine:
    """Unit tests for _store_price_line helper."""

    def test_price_drop(self):
        sa = {"payload": {"pct_drop": 25.0, "before": {"price_value": 200.0}, "after": {"price_value": 150.0}}}
        result = _store_price_line(sa, "PRICE_DROP")
        assert result == "$200.00 → $150.00 (-25%)"

    def test_clearance(self):
        sa = {"payload": {"percentage_off": 50, "after": {"price_value": 100.0, "percentage_off": 50}}}
        result = _store_price_line(sa, "CLEARANCE")
        assert result == "$100.00 (50% off)"

    def test_oos_returns_empty(self):
        sa = {"payload": {"before": {"in_stock": True}, "after": {"in_stock": False}}}
        assert _store_price_line(sa, "OOS") == ""
        assert _store_price_line(sa, "BACK_IN_STOCK") == ""


class TestPricesVary:
    """Unit tests for _prices_vary helper."""

    def test_different_prices(self):
        sas = [
            {"payload": {"after": {"price_value": 100.0, "percentage_off": 50}}},
            {"payload": {"after": {"price_value": 200.0, "percentage_off": 30}}},
        ]
        assert _prices_vary(sas, "CLEARANCE") is True

    def test_same_prices(self):
        sas = [
            {"payload": {"after": {"price_value": 100.0, "percentage_off": 50}}},
            {"payload": {"after": {"price_value": 100.0, "percentage_off": 50}}},
        ]
        assert _prices_vary(sas, "CLEARANCE") is False

    def test_oos_always_false(self):
        sas = [
            {"payload": {"before": {"in_stock": True}, "after": {"in_stock": False}}},
            {"payload": {"before": {"in_stock": False}, "after": {"in_stock": True}}},
        ]
        assert _prices_vary(sas, "OOS") is False
        assert _prices_vary(sas, "BACK_IN_STOCK") is False


class TestInferInStock:
    """Tests for infer_in_stock() helper."""

    def test_explicit_true(self):
        assert infer_in_stock({"in_stock": True, "inventory_qty": 5}) is True

    def test_explicit_false(self):
        """Explicit in_stock=False takes precedence over positive qty."""
        assert infer_in_stock({"in_stock": False, "inventory_qty": 5}) is False

    def test_infer_from_positive_qty(self):
        assert infer_in_stock({"in_stock": None, "inventory_qty": 5}) is True

    def test_infer_from_zero_qty(self):
        assert infer_in_stock({"in_stock": None, "inventory_qty": 0}) is False

    def test_both_none(self):
        assert infer_in_stock({"in_stock": None, "inventory_qty": None}) is None

    def test_missing_keys(self):
        assert infer_in_stock({}) is None

    def test_only_qty_present(self):
        assert infer_in_stock({"inventory_qty": 3}) is True


class TestStockBadgeRendering:
    """Regression tests: stock field must render when in_stock=None but inventory_qty exists."""

    def test_single_store_in_store_clearance_blocks(self):
        """Block Kit: IN_STORE_CLEARANCE with in_stock=None, qty=3 -> stock field present."""
        groups = [_make_group(
            alert_type="IN_STORE_CLEARANCE",
            in_stock=None,
            inventory_qty=3,
            store_alerts=[{
                "store_id": "2619",
                "payload": {
                    "clearance_value": 75.0,
                    "clearance_percentage_off": 50,
                    "after": {
                        "price_value": 149.0,
                        "in_stock": None,
                        "inventory_qty": 3,
                    },
                },
            }],
        )]
        groups[0]["payload"]["clearance_value"] = 75.0
        groups[0]["payload"]["clearance_percentage_off"] = 50
        blocks, _ = format_slack_blocks(groups)
        fields_block = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        assert fields_block
        field_texts = [f["text"] for f in fields_block[0]["fields"]]
        stock_fields = [t for t in field_texts if "*Stock:*" in t]
        assert len(stock_fields) == 1, "Stock field should render when in_stock=None but qty exists"
        assert "In Stock" in stock_fields[0]
        assert "3 units" in stock_fields[0]

    def test_single_store_in_store_clearance_text(self):
        """Plain text: IN_STORE_CLEARANCE with in_stock=None, qty=3 -> stock line present."""
        groups = [_make_group(
            alert_type="IN_STORE_CLEARANCE",
            in_stock=None,
            inventory_qty=3,
            store_alerts=[{
                "store_id": "2619",
                "payload": {
                    "clearance_value": 75.0,
                    "clearance_percentage_off": 50,
                    "after": {
                        "price_value": 149.0,
                        "in_stock": None,
                        "inventory_qty": 3,
                    },
                },
            }],
        )]
        groups[0]["payload"]["clearance_value"] = 75.0
        groups[0]["payload"]["clearance_percentage_off"] = 50
        msg = format_slack_message(groups)
        assert "Stock:" in msg, "Stock line should appear when in_stock=None but qty exists"
        assert "In Stock" in msg

    def test_zero_qty_renders_out_of_stock(self):
        """in_stock=None, qty=0 -> should render as Out of Stock, not omit."""
        groups = [_make_group(
            alert_type="IN_STORE_CLEARANCE",
            in_stock=None,
            inventory_qty=0,
            store_alerts=[{
                "store_id": "2619",
                "payload": {
                    "clearance_value": 75.0,
                    "after": {
                        "price_value": 149.0,
                        "in_stock": None,
                        "inventory_qty": 0,
                    },
                },
            }],
        )]
        groups[0]["payload"]["clearance_value"] = 75.0
        msg = format_slack_message(groups)
        assert "Stock:" in msg
        assert "Out of Stock" in msg
