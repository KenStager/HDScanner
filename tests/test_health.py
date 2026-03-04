"""Tests for health/drift detection."""

from __future__ import annotations

import pytest

from hd.pipeline.health import check_drift, HealthStatus, _resolve_path


class TestCheckDrift:
    def test_healthy_when_all_paths_present(self):
        products = [
            {
                "pricing": {
                    "value": 100,
                    "original": 120,
                    "promotion": {
                        "savingsCenter": "CLEARANCE",
                    },
                },
                "fulfillment": {
                    "fulfillmentOptions": [{"type": "pickup"}],
                },
                "identifiers": {
                    "brandName": "Milwaukee",
                    "productLabel": "M18 FUEL",
                },
            }
            for _ in range(10)
        ]

        status, failed = check_drift(products)
        assert status == HealthStatus.HEALTHY
        assert failed == []

    def test_degraded_when_pricing_missing(self):
        # All products missing pricing.value
        products = [
            {
                "pricing": {
                    "original": 120,
                    "promotion": {
                        "savingsCenter": "CLEARANCE",
                    },
                },
                "fulfillment": {
                    "fulfillmentOptions": [{"type": "pickup"}],
                },
                "identifiers": {
                    "brandName": "Milwaukee",
                    "productLabel": "M18 FUEL",
                },
            }
            for _ in range(10)
        ]

        status, failed = check_drift(products)
        assert status == HealthStatus.DEGRADED
        assert any("pricing.value" in f for f in failed)

    def test_degraded_when_identifiers_missing(self):
        # All products missing identifiers.brandName
        products = [
            {
                "pricing": {
                    "value": 100,
                    "original": 120,
                    "promotion": {
                        "savingsCenter": "CLEARANCE",
                    },
                },
                "fulfillment": {
                    "fulfillmentOptions": [{"type": "pickup"}],
                },
                "identifiers": {
                    "productLabel": "M18 FUEL",
                },
            }
            for _ in range(10)
        ]

        status, failed = check_drift(products)
        assert status == HealthStatus.DEGRADED
        assert any("identifiers.brandName" in f for f in failed)

    def test_healthy_when_below_threshold(self):
        # Only 3 out of 10 missing pricing.value — 30% < 50% threshold
        good = {
            "pricing": {"value": 100, "original": 120, "promotion": {"savingsCenter": "X"}},
            "fulfillment": {"fulfillmentOptions": [{}]},
            "identifiers": {"brandName": "Milwaukee", "productLabel": "Foo"},
        }
        bad = {
            "pricing": {"original": 120},
            "fulfillment": {"fulfillmentOptions": [{}]},
            "identifiers": {"brandName": "Milwaukee", "productLabel": "Foo"},
        }
        products = [good] * 7 + [bad] * 3

        status, failed = check_drift(products)
        assert status == HealthStatus.HEALTHY

    def test_degraded_on_empty_products(self):
        status, failed = check_drift([])
        assert status == HealthStatus.DEGRADED

    def test_missing_original_does_not_degrade(self):
        """pricing.original should not be a critical path."""
        products = [
            {
                "pricing": {"value": 100, "promotion": {"savingsCenter": "X"}},
                "fulfillment": {"fulfillmentOptions": [{}]},
                "identifiers": {"brandName": "Milwaukee", "productLabel": "Foo"},
            }
            for _ in range(10)
        ]
        status, failed = check_drift(products)
        assert status == HealthStatus.HEALTHY

    def test_custom_threshold(self):
        # 6 out of 10 missing — 60% > 50% default, but < 80% custom
        good = {
            "pricing": {"value": 100, "original": 120, "promotion": {"savingsCenter": "X"}},
            "fulfillment": {"fulfillmentOptions": [{}]},
            "identifiers": {"brandName": "Milwaukee", "productLabel": "Foo"},
        }
        bad = {
            "pricing": {"original": 120},
            "fulfillment": {"fulfillmentOptions": [{}]},
            "identifiers": {"brandName": "Milwaukee", "productLabel": "Foo"},
        }
        products = [good] * 4 + [bad] * 6

        status, _ = check_drift(products, threshold_pct=80)
        assert status == HealthStatus.HEALTHY


class TestResolvePath:
    def test_simple_path(self):
        obj = {"pricing": {"value": 100}}
        assert _resolve_path(obj, "pricing.value") == 100

    def test_nested_path(self):
        obj = {"pricing": {"promotion": {"savingsCenter": "CLEARANCE"}}}
        assert _resolve_path(obj, "pricing.promotion.savingsCenter") == "CLEARANCE"

    def test_missing_path(self):
        obj = {"pricing": {}}
        assert _resolve_path(obj, "pricing.value") is None

    def test_none_input(self):
        assert _resolve_path(None, "pricing.value") is None

    def test_non_dict_intermediate(self):
        obj = {"pricing": "not_a_dict"}
        assert _resolve_path(obj, "pricing.value") is None
