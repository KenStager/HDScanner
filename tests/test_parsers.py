"""Tests for API response parsers."""

from __future__ import annotations

import pytest

from hd.hd_api.parsers import parse_products, parse_snapshots, matches_product_line
from hd.hd_api.models import NormalizedProduct


class TestParseProducts:
    def test_parses_products_from_fixture(self, sample_response):
        products = parse_products(sample_response)
        assert len(products) == 3

        # First product — M18 FUEL impact wrench
        p = products[0]
        assert p.item_id == "312345678"
        assert p.brand == "Milwaukee"
        assert "M18 FUEL" in p.title
        assert p.model_number == "2767-20"
        assert p.canonical_url is not None

    def test_parses_product_with_no_promotion(self, sample_response):
        products = parse_products(sample_response)
        p = products[1]  # M12 product with no promotion
        assert p.item_id == "312345679"
        assert "M12 FUEL" in p.title

    def test_handles_empty_response(self):
        products = parse_products({})
        assert products == []

    def test_handles_none_data(self):
        products = parse_products({"data": None})
        assert products == []

    def test_handles_missing_searchModel(self):
        products = parse_products({"data": {}})
        assert products == []

    def test_handles_null_product_in_list(self):
        response = {
            "data": {
                "searchModel": {
                    "products": [None, {"itemId": "123", "identifiers": {}}]
                }
            }
        }
        products = parse_products(response)
        assert len(products) == 1


class TestParseSnapshots:
    def test_parses_snapshots_with_inventory(self, sample_response):
        snapshots = parse_snapshots(sample_response, "2619")
        assert len(snapshots) == 3

        # First product has clearance pricing and store 2619 inventory
        s = snapshots[0]
        assert s.item_id == "312345678"
        assert s.store_id == "2619"
        assert s.price_value == 249.00
        assert s.price_original == 299.00
        assert s.savings_center == "CLEARANCE"
        assert s.promotion_tag == "Clearance"
        assert s.percentage_off == 17
        assert s.dollar_off == 50.00
        assert s.inventory_qty == 12
        assert s.in_stock is True
        assert s.out_of_stock is False

    def test_parses_snapshot_no_promotion(self, sample_response):
        snapshots = parse_snapshots(sample_response, "2619")
        s = snapshots[1]  # No promotion
        assert s.promotion_type is None
        assert s.savings_center is None

    def test_parses_snapshot_no_fulfillment(self, sample_response):
        snapshots = parse_snapshots(sample_response, "2619")
        s = snapshots[2]  # PACKOUT box — no fulfillment data
        assert s.inventory_qty is None
        assert s.in_stock is None

    def test_wrong_store_id_no_inventory(self, sample_response):
        snapshots = parse_snapshots(sample_response, "9999")
        s = snapshots[0]
        assert s.inventory_qty is None

    def test_handles_empty_response(self):
        snapshots = parse_snapshots({}, "2619")
        assert snapshots == []


class TestMatchesProductLine:
    def test_matches_m18_in_title(self):
        p = NormalizedProduct(item_id="1", title="Milwaukee M18 FUEL Impact", model_number="2767-20")
        assert matches_product_line(p, ["M12", "M18"]) is True

    def test_matches_m12_in_title(self):
        p = NormalizedProduct(item_id="1", title="Milwaukee M12 FUEL Stubby", model_number="2554-20")
        assert matches_product_line(p, ["M12", "M18"]) is True

    def test_rejects_non_m12_m18(self):
        p = NormalizedProduct(item_id="1", title="Milwaukee PACKOUT 22 in. Tool Box", model_number="48-22-8424")
        assert matches_product_line(p, ["M12", "M18"]) is False

    def test_rejects_hand_tools(self):
        p = NormalizedProduct(item_id="1", title="Milwaukee 25 ft. Tape Measure", model_number="48-22-6825")
        assert matches_product_line(p, ["M12", "M18"]) is False

    def test_empty_filters_matches_all(self):
        p = NormalizedProduct(item_id="1", title="Anything")
        assert matches_product_line(p, []) is True

    def test_matches_case_insensitive(self):
        p = NormalizedProduct(item_id="1", title="milwaukee m18 fuel kit")
        assert matches_product_line(p, ["M18"]) is True

    def test_handles_none_title(self):
        p = NormalizedProduct(item_id="1", title=None, model_number="2767-20")
        # model_number doesn't contain M12 or M18
        assert matches_product_line(p, ["M12", "M18"]) is False

    def test_handles_none_both(self):
        p = NormalizedProduct(item_id="1", title=None, model_number=None)
        assert matches_product_line(p, ["M12", "M18"]) is False
