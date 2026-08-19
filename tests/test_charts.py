"""Tests for the chart option builders — the online-price merge in particular."""

from __future__ import annotations

from datetime import datetime, timedelta

from hd.dashboard.components.charts import (
    ONLINE_SERIES_COLOR,
    online_prices_agree,
    price_history_options,
)

T0 = datetime(2026, 8, 17, 12, 0, 0)


def _snap(store_id: str, minutes: int, price: float | None,
          clearance: float | None = None) -> dict:
    return {
        "store_id": store_id,
        "ts": T0 + timedelta(minutes=minutes),
        "price_value": price,
        "clearance_value": clearance,
    }


class TestOnlinePricesAgree:
    def test_identical_series_agree(self):
        snaps = [_snap("2619", 0, 59.97), _snap("8452", 5, 59.97),
                 _snap("2619", 240, 59.97), _snap("8452", 245, 59.97)]
        assert online_prices_agree(snaps, ["2619", "8452"]) is True

    def test_divergence_in_shared_hour_disagrees(self):
        """A store-localized price must keep per-store series."""
        snaps = [_snap("2619", 0, 20.65), _snap("8452", 5, 59.00)]
        assert online_prices_agree(snaps, ["2619", "8452"]) is False

    def test_single_store_trivially_agrees(self):
        snaps = [_snap("2619", 0, 129.00), _snap("2619", 240, 129.00)]
        assert online_prices_agree(snaps, ["2619", "8452"]) is True

    def test_shared_transition_agrees(self):
        """Both stores seeing the same price drop in the same hour is one story."""
        snaps = [_snap("2619", 0, 199.00), _snap("8452", 5, 199.00),
                 _snap("2619", 50, 149.00), _snap("8452", 55, 149.00)]
        assert online_prices_agree(snaps, ["2619", "8452"]) is True

    def test_unpriced_and_foreign_stores_ignored(self):
        snaps = [_snap("2619", 0, 59.97), _snap("8452", 5, None),
                 _snap("9999", 5, 1.00)]
        assert online_prices_agree(snaps, ["2619", "8452"]) is True


class TestPriceHistoryOptions:
    NAMES = {"2619": "Greenfield", "8452": "Hadley"}

    def test_agreeing_stores_merge_into_one_online_series(self):
        snaps = [_snap("2619", 0, 59.97), _snap("8452", 5, 59.97)]
        opts = price_history_options(snaps, ["2619", "8452"], self.NAMES)
        names = [s["name"] for s in opts["series"]]
        assert names == ["Online price"]
        assert opts["series"][0]["itemStyle"]["color"] == ONLINE_SERIES_COLOR
        # The merged series carries both stores' readings
        assert len(opts["series"][0]["data"]) == 2

    def test_diverging_stores_keep_per_store_series(self):
        snaps = [_snap("2619", 0, 20.65), _snap("8452", 5, 59.00)]
        opts = price_history_options(snaps, ["2619", "8452"], self.NAMES)
        names = [s["name"] for s in opts["series"]]
        assert names == ["Greenfield", "Hadley"]

    def test_clearance_series_stays_per_store_when_merged(self):
        snaps = [_snap("2619", 0, 59.97, clearance=21.00), _snap("8452", 5, 59.97)]
        opts = price_history_options(snaps, ["2619", "8452"], self.NAMES)
        names = [s["name"] for s in opts["series"]]
        assert names == ["Online price", "Greenfield clearance"]

    def test_low_anchor_draws_reference_line(self):
        snaps = [_snap("2619", 0, 169.00), _snap("8452", 5, 169.00)]
        opts = price_history_options(
            snaps, ["2619", "8452"], self.NAMES,
            low_anchor=(99.00, "seen $99.00 · May 15"),
        )
        mark = opts["series"][0]["markLine"]
        assert mark["data"][0]["yAxis"] == 99.00
