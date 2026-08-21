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
async def fresh_db():
    from hd.db import base

    db = base.Database()
    with patch.object(base, "_default", db):
        yield db
    # conftest's autouse teardown closes whatever `base._default` is by then,
    # which patch.object has already restored to the original — not this one.
    # An undisposed engine leaves an aiosqlite worker thread running past the
    # loop that owns it, and it surfaces as "Event loop is closed" against
    # whichever unrelated test happens to be running when it dies.
    await db.close_db()


async def seed_catalog(settings, **brands_by_item):
    """Put items in the products table so the brand gate can recognise them."""
    from datetime import datetime, timezone
    from hd.db import base
    from hd.db.models import Product

    async with base.get_session(settings) as session:
        now = datetime.now(timezone.utc)
        for item_id, brand in brands_by_item.items():
            session.add(Product(
                item_id=item_id, brand=brand, title=f"item {item_id}",
                first_seen_ts=now, last_seen_ts=now,
            ))
        await session.commit()


class TestRunDailyDeals:
    async def test_sweep_prices_brand_matches_and_sets_cursor(self, dd_settings, fresh_db):
        from hd.db import base
        from hd.db.models import Product, StoreSnapshot

        await base.init_db(dd_settings)
        # 111 and 222 are already tracked as ours; 333 is a brand we do not want.
        await seed_catalog(dd_settings, **{"111": "Milwaukee", "222": "Milwaukee", "333": "RYOBI"})
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=["111", "222", "333"])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary = await run_daily_deals(dd_settings, client=client)

        assert summary.skipped is False
        # 333 costs nothing now: the catalog already says it is not ours, so it
        # is never requested rather than requested and then discarded.
        assert client.requested == ["111", "222"]
        assert summary.items_checked == 2
        assert summary.brand_matches == 2
        assert summary.snapshots == 2
        assert summary.skipped_unknown == 1

        async with base.get_session(dd_settings) as session:
            prods = {p.item_id for p in (await session.execute(select(Product))).scalars().all()}
            snaps = [(s.item_id, s.store_id) for s in
                     (await session.execute(select(StoreSnapshot))).scalars().all()]
        # 333 is present only because the test seeded it; the sweep neither
        # requested it nor snapshotted it.
        assert prods == {"111", "222", "333"}
        assert set(snaps) == {("111", "2619"), ("222", "2619")}

        # The matches are recorded as picks so the dashboard can pin the set.
        from hd.db.models import DailyDealPick
        async with base.get_session(dd_settings) as session:
            picks = {(p.end_date, p.item_id) for p in
                     (await session.execute(select(DailyDealPick))).scalars().all()}
        assert picks == {("2026-08-18", "111"), ("2026-08-18", "222")}

        # Second run same day: cursor short-circuits before any API traffic
        client2 = FakeClient()
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary2 = await run_daily_deals(dd_settings, client=client2)
        assert summary2.skipped is True
        assert client2.requested == []

    async def test_new_end_date_triggers_new_sweep(self, dd_settings, fresh_db):
        from hd.db import base

        await base.init_db(dd_settings)
        await seed_catalog(dd_settings, **{"111": "Milwaukee", "222": "Milwaukee"})
        day1 = DailyDealSet(end_date="2026-08-18", item_ids=["111"])
        day2 = DailyDealSet(end_date="2026-08-19", item_ids=["222"])

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=day1):
            await run_daily_deals(dd_settings, client=FakeClient())
        client = FakeClient()
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=day2):
            summary = await run_daily_deals(dd_settings, client=client)
        assert summary.skipped is False
        assert client.requested == ["222"]

    async def test_set_with_no_tracked_brands_costs_nothing(self, dd_settings, fresh_db):
        """The measured case: ~110 patio and garden items, none of them ours."""
        from hd.db import base

        await base.init_db(dd_settings)
        await seed_catalog(dd_settings, **{"999": "Milwaukee"})  # tracked, but not on offer
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=[str(i) for i in range(100, 210)])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary = await run_daily_deals(dd_settings, client=client)

        assert client.requested == []          # 110 requests saved
        assert summary.items_checked == 0
        assert summary.skipped_unknown == 110
        # The day is still recorded, so the next run does not re-check it.
        from hd.pipeline.daily_deals import _read_cursor
        assert _read_cursor(dd_settings.daily_deals_cursor_path) == "2026-08-18"

    async def test_probe_budget_allows_checking_unknown_items(self, dd_settings, fresh_db):
        """Opt-in escape hatch: a brand item we have never seen is invisible to the gate."""
        from hd.db import base

        await base.init_db(dd_settings)
        dd_settings.daily_deals_probe_unknown = 2
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=["111", "222", "333"])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary = await run_daily_deals(dd_settings, client=client)

        assert client.requested == ["111", "222"]   # bounded by the probe budget
        assert summary.skipped_unknown == 1

    async def test_unfetchable_page_skips_quietly(self, dd_settings, fresh_db):
        from hd.db import base

        await base.init_db(dd_settings)
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=None):
            summary = await run_daily_deals(dd_settings, client=FakeClient())
        assert summary.skipped is True
