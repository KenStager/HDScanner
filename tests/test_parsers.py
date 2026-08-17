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


class TestBossOnlyInventory:
    """BOSS-only items should be marked OOS — HD shows them as unavailable."""

    def test_boss_only_reports_out_of_stock(self):
        """Item with only BOSS fulfillment (no BOPIS/express) should be OOS."""
        response = {
            "data": {
                "searchModel": {
                    "products": [
                        {
                            "itemId": "333177561",
                            "identifiers": {"brandName": "Milwaukee", "modelNumber": "TEST-BOSS"},
                            "media": {"images": [{"url": "https://example.com/img.jpg"}]},
                            "pricing": {"value": 99.00},
                            "fulfillment": {
                                "fulfillmentOptions": [
                                    {
                                        "services": [
                                            {
                                                "type": "boss",
                                                "locations": [
                                                    {
                                                        "locationId": "2619",
                                                        "type": "online",
                                                        "inventory": {
                                                            "isOutOfStock": False,
                                                            "isInStock": True,
                                                            "isLimitedQuantity": False,
                                                            "isUnavailable": False,
                                                            "quantity": 1,
                                                        },
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
        snapshots = parse_snapshots(response, "2619")
        assert len(snapshots) == 1
        s = snapshots[0]
        assert s.in_stock is False
        assert s.out_of_stock is True
        assert s.inventory_qty is None


class TestClearanceParsing:
    """Tests for in-store clearance field parsing."""

    def test_parses_clearance_fields(self, sample_response):
        """M12 Stubby fixture has clearance pricing."""
        snapshots = parse_snapshots(sample_response, "2619")
        s = snapshots[1]  # M12 Stubby with clearance
        assert s.clearance_value == 99.00
        assert s.clearance_dollar_off == 60.00
        assert s.clearance_percentage_off == 38

    def test_clearance_null_when_absent(self, sample_response):
        """M18 FUEL has no clearance object."""
        snapshots = parse_snapshots(sample_response, "2619")
        s = snapshots[0]  # M18 FUEL — has promotion but no clearance
        assert s.clearance_value is None
        assert s.clearance_dollar_off is None
        assert s.clearance_percentage_off is None

    def test_clearance_null_for_packout(self, sample_response):
        """PACKOUT box has no clearance."""
        snapshots = parse_snapshots(sample_response, "2619")
        s = snapshots[2]  # PACKOUT — no clearance
        assert s.clearance_value is None


class TestBopisOosFallback:
    """When BOPIS is OOS but express delivery has stock, prefer express delivery."""

    def test_bopis_oos_uses_express_delivery(self, sample_response):
        """M12 Stubby fixture: BOPIS OOS + express delivery in stock."""
        snapshots = parse_snapshots(sample_response, "2619")
        s = snapshots[1]  # M12 Stubby with split inventory
        assert s.in_stock is True
        assert s.inventory_qty == 3
        assert s.out_of_stock is False

    def test_bopis_in_stock_still_preferred(self, sample_response):
        """M18 FUEL fixture: BOPIS in stock should be used."""
        snapshots = parse_snapshots(sample_response, "2619")
        s = snapshots[0]
        assert s.in_stock is True
        assert s.inventory_qty == 12


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


class TestHasAnyFulfillment:
    def _item(self, locations):
        return {
            "fulfillment": {
                "fulfillmentOptions": [{
                    "type": "delivery",
                    "services": [{"type": "sth", "locations": locations}],
                }],
            }
        }

    def test_in_stock_location(self):
        from hd.hd_api.parsers import has_any_fulfillment
        item = self._item([{"locationId": "x", "inventory": {"isInStock": True}}])
        assert has_any_fulfillment(item) is True

    def test_quantity_only(self):
        from hd.hd_api.parsers import has_any_fulfillment
        item = self._item([{"locationId": "x", "inventory": {"quantity": 4}}])
        assert has_any_fulfillment(item) is True

    def test_all_locations_oos(self):
        from hd.hd_api.parsers import has_any_fulfillment
        item = self._item([
            {"locationId": "x", "inventory": {"isInStock": False, "isOutOfStock": True}},
            {"locationId": "y", "inventory": {"isInStock": False}},
        ])
        assert has_any_fulfillment(item) is False

    def test_no_fulfillment_data_is_unknown(self):
        from hd.hd_api.parsers import has_any_fulfillment
        assert has_any_fulfillment({"fulfillment": None}) is None
        assert has_any_fulfillment({}) is None
        assert has_any_fulfillment(None) is None

    def test_null_safe_on_malformed(self):
        from hd.hd_api.parsers import has_any_fulfillment
        item = {"fulfillment": {"fulfillmentOptions": [None, {"services": [None, {"locations": [None]}]}]}}
        assert has_any_fulfillment(item) is None
