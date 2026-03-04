"""Tests for alert grouping logic in the alerts page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hd.grouping import (
    GROUP_WINDOW_MINUTES as _GROUP_WINDOW_MINUTES,
    build_group as _build_group,
    group_alerts as _group_alerts,
    parse_ts as _parse_ts,
)


def _make_alert(
    *,
    id: int = 1,
    item_id: str = "315442497",
    alert_type: str = "PRICE_DROP",
    store_id: str = "2619",
    severity: str = "high",
    ts: datetime | None = None,
    pct_drop: float | None = 15.0,
    price_before: float = 299.0,
    price_after: float = 249.0,
) -> dict:
    if ts is None:
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
    return {
        "id": id,
        "item_id": item_id,
        "alert_type": alert_type,
        "store_id": store_id,
        "severity": severity,
        "ts": ts,
        "product_title": f"Test Product {item_id}",
        "payload": {
            "pct_drop": pct_drop,
            "before": {"price_value": price_before, "in_stock": True},
            "after": {"price_value": price_after, "in_stock": True},
            "product_url": f"https://homedepot.com/p/{item_id}",
        },
    }


# ---------------------------------------------------------------------------
# _parse_ts
# ---------------------------------------------------------------------------


class TestParseTs:
    def test_none_returns_epoch(self):
        result = _parse_ts(None)
        assert result.year == 2000

    def test_naive_datetime_becomes_utc(self):
        dt = datetime(2026, 1, 1, 12, 0)
        result = _parse_ts(dt)
        assert result.tzinfo == timezone.utc

    def test_aware_datetime_unchanged(self):
        dt = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        result = _parse_ts(dt)
        assert result == dt

    def test_isoformat_string(self):
        result = _parse_ts("2026-03-04T10:00:00+00:00")
        assert result.year == 2026
        assert result.month == 3


# ---------------------------------------------------------------------------
# _build_group
# ---------------------------------------------------------------------------


class TestBuildGroup:
    def test_single_alert_group(self):
        a = _make_alert(id=1, store_id="2619")
        group = _build_group([a])
        assert group["store_count"] == 1
        assert group["store_ids_display"] == "2619"
        assert group["item_id"] == "315442497"
        assert group["alert_type"] == "PRICE_DROP"
        assert group["group_key"] == "315442497_PRICE_DROP_1"

    def test_two_store_group(self):
        base_ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", severity="medium", pct_drop=10.0, ts=base_ts)
        a2 = _make_alert(id=2, store_id="8425", severity="high", pct_drop=20.0,
                         ts=base_ts + timedelta(minutes=2))
        group = _build_group([a1, a2])

        assert group["store_count"] == 2
        assert group["store_ids_display"] == "2619, 8425"
        # Representative should be high severity with 20% drop
        assert group["severity"] == "high"
        assert group["payload"]["pct_drop"] == 20.0

    def test_representative_picks_highest_severity(self):
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        low = _make_alert(id=1, store_id="2619", severity="low", pct_drop=5.0, ts=ts)
        high = _make_alert(id=2, store_id="8425", severity="high", pct_drop=5.0, ts=ts)
        group = _build_group([low, high])
        assert group["severity"] == "high"

    def test_representative_tiebreaks_on_pct_drop(self):
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", severity="high", pct_drop=10.0, ts=ts)
        a2 = _make_alert(id=2, store_id="8425", severity="high", pct_drop=25.0, ts=ts)
        group = _build_group([a1, a2])
        assert group["payload"]["pct_drop"] == 25.0

    def test_most_recent_timestamp_used(self):
        t1 = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 4, 10, 5, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", ts=t1)
        a2 = _make_alert(id=2, store_id="8425", ts=t2)
        group = _build_group([a1, a2])
        assert group["ts"] == t2

    def test_store_alerts_preserved(self):
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", ts=ts)
        a2 = _make_alert(id=2, store_id="8425", ts=ts)
        group = _build_group([a1, a2])
        assert len(group["store_alerts"]) == 2


# ---------------------------------------------------------------------------
# _group_alerts
# ---------------------------------------------------------------------------


class TestGroupAlerts:
    def test_empty_list(self):
        assert _group_alerts([]) == []

    def test_single_alert_becomes_single_group(self):
        a = _make_alert(id=1)
        groups = _group_alerts([a])
        assert len(groups) == 1
        assert groups[0]["store_count"] == 1

    def test_same_item_two_stores_within_window_grouped(self):
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", ts=ts)
        a2 = _make_alert(id=2, store_id="8425", ts=ts + timedelta(minutes=3))
        groups = _group_alerts([a1, a2])

        assert len(groups) == 1
        assert groups[0]["store_count"] == 2
        assert "2619" in groups[0]["store_ids_display"]
        assert "8425" in groups[0]["store_ids_display"]

    def test_same_item_outside_window_not_grouped(self):
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", ts=ts)
        a2 = _make_alert(
            id=2, store_id="8425",
            ts=ts + timedelta(minutes=_GROUP_WINDOW_MINUTES + 1),
        )
        groups = _group_alerts([a1, a2])
        assert len(groups) == 2

    def test_different_items_not_grouped(self):
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, item_id="111", store_id="2619", ts=ts)
        a2 = _make_alert(id=2, item_id="222", store_id="8425", ts=ts)
        groups = _group_alerts([a1, a2])
        assert len(groups) == 2

    def test_different_alert_types_not_grouped(self):
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, alert_type="PRICE_DROP", ts=ts)
        a2 = _make_alert(id=2, alert_type="CLEARANCE", ts=ts)
        groups = _group_alerts([a1, a2])
        assert len(groups) == 2

    def test_sorted_most_recent_first(self):
        t1 = datetime(2026, 3, 4, 8, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 4, 12, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, item_id="111", ts=t1)
        a2 = _make_alert(id=2, item_id="222", ts=t2)
        groups = _group_alerts([a1, a2])
        assert groups[0]["item_id"] == "222"  # more recent first
        assert groups[1]["item_id"] == "111"

    def test_boundary_exactly_at_window_grouped(self):
        """Alerts exactly 10 minutes apart should still be grouped (<=)."""
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", ts=ts)
        a2 = _make_alert(
            id=2, store_id="8425",
            ts=ts + timedelta(minutes=_GROUP_WINDOW_MINUTES),
        )
        groups = _group_alerts([a1, a2])
        assert len(groups) == 1

    def test_three_stores_chained_window(self):
        """Three alerts each 5 min apart — all within chain window."""
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=1, store_id="2619", ts=ts)
        a2 = _make_alert(id=2, store_id="8425", ts=ts + timedelta(minutes=5))
        a3 = _make_alert(id=3, store_id="9999", ts=ts + timedelta(minutes=10))
        groups = _group_alerts([a1, a2, a3])
        assert len(groups) == 1
        assert groups[0]["store_count"] == 3

    def test_mixed_scenario(self):
        """Two items, each with 2 stores — should produce 2 groups."""
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        alerts = [
            _make_alert(id=1, item_id="AAA", store_id="2619", ts=ts),
            _make_alert(id=2, item_id="AAA", store_id="8425", ts=ts + timedelta(minutes=1)),
            _make_alert(id=3, item_id="BBB", store_id="2619", ts=ts),
            _make_alert(id=4, item_id="BBB", store_id="8425", ts=ts + timedelta(minutes=2)),
        ]
        groups = _group_alerts(alerts)
        assert len(groups) == 2
        for g in groups:
            assert g["store_count"] == 2

    def test_null_safe_payload(self):
        """Alerts with None payload should not crash grouping."""
        a = _make_alert(id=1)
        a["payload"] = None
        groups = _group_alerts([a])
        assert len(groups) == 1

    def test_group_key_stable(self):
        """Group key uses earliest alert id for stability."""
        ts = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone.utc)
        a1 = _make_alert(id=7, store_id="2619", ts=ts)
        a2 = _make_alert(id=12, store_id="8425", ts=ts + timedelta(minutes=1))
        groups = _group_alerts([a1, a2])
        assert groups[0]["group_key"] == "315442497_PRICE_DROP_7"
