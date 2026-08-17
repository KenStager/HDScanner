"""Tests for the Daily Deals sweep."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from hd.config import Settings
from hd.pipeline.daily_deals import (
    DailyDealSet,
    parse_daily_deal_page,
    run_daily_deals,
)


def make_page(end_date="2026-08-18", categories=None) -> str:
    if categories is None:
        categories = [
            {"__typename": "CategoryMetadata", "name": "Power Tool Kits",
             "tagline": "Up to 55% off", "itemIds": ["111", "222"]},
            {"__typename": "CategoryMetadata", "name": "Hand Tools",
             "tagline": "Up to 40% off", "itemIds": ["222", "333"]},
        ]
    state = {
        "ROOT_QUERY": {
            "__typename": "Query",
            'specialBuyMetadata({\\"backupCategories\\":true,\\"dealType\\":\\"DAY\\",\\"previewDate\\":null})': {
                "__typename": "SpecialBuyResponse",
                "endDate": end_date,
                "categoryMetadata": categories,
            },
            'specialBuyMetadata({\\"dealType\\":\\"WEEK\\",\\"previewDate\\":null})': {
                "__typename": "SpecialBuyResponse",
                "endDate": "2026-08-24",
                "categoryMetadata": [
                    {"__typename": "CategoryMetadata", "name": "Weekly", "itemIds": ["999"]},
                ],
            },
        }
    }
    return f"<html><script>window.__APOLLO_STATE__={json.dumps(state)};</script></html>"


class TestParseDailyDealPage:
    def test_extracts_day_set_with_dedup(self):
        result = parse_daily_deal_page(make_page())
        assert result is not None
        assert result.end_date == "2026-08-18"
        assert result.item_ids == ["111", "222", "333"]  # deduped, order kept
        assert [c["name"] for c in result.categories] == ["Power Tool Kits", "Hand Tools"]

    def test_week_set_ignored(self):
        result = parse_daily_deal_page(make_page())
        assert "999" not in result.item_ids

    def test_no_marker_returns_none(self):
        assert parse_daily_deal_page("<html>nothing here</html>") is None

    def test_malformed_json_returns_none(self):
        assert parse_daily_deal_page("window.__APOLLO_STATE__={broken") is None


def _search_response(item_id: str, brand: str = "Milwaukee") -> dict:
    return {
        "data": {"searchModel": {
            "searchReport": {"totalProducts": 1},
            "products": [{
                "itemId": item_id,
                "identifiers": {"brandName": brand, "modelNumber": f"M-{item_id}",
                                "productLabel": f"Product {item_id}", "canonicalUrl": f"/p/{item_id}"},
                "pricing": {"value": 99.0, "original": 199.0,
                            "promotion": {"percentageOff": 50}, "clearance": None,
                            "specialBuy": 99.0},
                "media": {"images": []},
                "fulfillment": {"fulfillmentOptions": [{"type": "delivery", "services": [{
                    "type": "sth", "locations": [{"locationId": "8119",
                                                  "inventory": {"quantity": 10, "isInStock": True}}],
                }]}]},
            }],
        }}
    }


class FakeClient:
    def __init__(self):
        self.requested: list[str] = []

    async def post_graphql(self, variables):
        item_id = variables["keyword"]
        self.requested.append(item_id)
        brand = "Milwaukee" if item_id != "333" else "RYOBI"
        return _search_response(item_id, brand)

    @property
    def is_throttled(self):
        return False

    @property
    def request_count(self):
        return len(self.requested)

    @property
    def failures(self):
        return {}

    async def close(self):
        pass


@pytest.fixture
def dd_settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/dd.db",
        stores="2619,8452",
        brands="Milwaukee",
        daily_deals_cursor_path=str(tmp_path / "dd_cursor"),
        store_raw_json=False,
    )


@pytest.fixture
def fresh_db():
    from hd.db import base

    db = base.Database()
    with patch.object(base, "_default", db):
        yield db


class TestRunDailyDeals:
    async def test_sweep_prices_brand_matches_and_sets_cursor(self, dd_settings, fresh_db):
        from hd.db import base
        from hd.db.models import Product, StoreSnapshot

        await base.init_db(dd_settings)
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=["111", "222", "333"])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary = await run_daily_deals(dd_settings, client=client)

        assert summary.skipped is False
        assert summary.items_checked == 3
        assert summary.brand_matches == 2   # 333 is RYOBI — filtered
        assert summary.snapshots == 2

        async with base.get_session(dd_settings) as session:
            prods = {p.item_id for p in (await session.execute(select(Product))).scalars().all()}
            snaps = [(s.item_id, s.store_id) for s in
                     (await session.execute(select(StoreSnapshot))).scalars().all()]
        assert prods == {"111", "222"}
        assert set(snaps) == {("111", "2619"), ("222", "2619")}

        # Second run same day: cursor short-circuits before any API traffic
        client2 = FakeClient()
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary2 = await run_daily_deals(dd_settings, client=client2)
        assert summary2.skipped is True
        assert client2.requested == []

    async def test_new_end_date_triggers_new_sweep(self, dd_settings, fresh_db):
        from hd.db import base

        await base.init_db(dd_settings)
        day1 = DailyDealSet(end_date="2026-08-18", item_ids=["111"])
        day2 = DailyDealSet(end_date="2026-08-19", item_ids=["222"])

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=day1):
            await run_daily_deals(dd_settings, client=FakeClient())
        client = FakeClient()
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=day2):
            summary = await run_daily_deals(dd_settings, client=client)
        assert summary.skipped is False
        assert client.requested == ["222"]

    async def test_unfetchable_page_skips_quietly(self, dd_settings, fresh_db):
        from hd.db import base

        await base.init_db(dd_settings)
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=None):
            summary = await run_daily_deals(dd_settings, client=FakeClient())
        assert summary.skipped is True
