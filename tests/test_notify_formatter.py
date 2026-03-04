"""Tests for Slack notification formatter."""

from __future__ import annotations

from datetime import datetime, timezone

from hd.notifiers.formatter import format_slack_message


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
        assert "*2 new clearance alerts*" in msg

    def test_header_count(self):
        groups = [_make_group()]
        msg = format_slack_message(groups)
        assert "*1 new clearance alert*" in msg
        # Singular — no trailing 's'
        assert "*1 new clearance alerts*" not in msg

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
