"""Tests for the facet-driven brand browse pipeline."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from hd.config import Settings
from hd.hd_api.parsers import parse_dimensions
from hd.pipeline.browse import (
    Walk,
    build_nav,
    plan_walks,
    reachable_cap,
    resolve_walks,
    run_browse,
    walk_and_capture,
)


# ---------------------------------------------------------------- helpers

def make_product(item_id: str, brand: str = "Milwaukee", clearance: dict | None = None,
                 qty: int = 5, store: str = "2619") -> dict:
    return {
        "itemId": item_id,
        "identifiers": {
            "brandName": brand,
            "modelNumber": f"M-{item_id}",
            "productLabel": f"Product {item_id}",
            "canonicalUrl": f"/p/{item_id}",
        },
        "pricing": {
            "value": 100.0,
            "original": 100.0,
            "promotion": {},
            "clearance": clearance,
        },
        "media": {"images": []},
        "fulfillment": {
            "fulfillmentOptions": [{
                "type": "pickup",
                "services": [{
                    "type": "bopis",
                    "locations": [{
                        "locationId": store,
                        "inventory": {"quantity": qty, "isInStock": qty > 0},
                    }],
                }],
            }],
        },
    }


def make_page(products: list[dict], total: int, dims: list[dict] | None = None) -> dict:
    return {
        "data": {
            "searchModel": {
                "searchReport": {"totalProducts": total},
                "dimensions": dims or [],
                "products": products,
            }
        }
    }


def cat_dim(*refs: tuple[str, str, int]) -> dict:
    return {
        "label": "Category",
        "refinements": [
            {"label": label, "refinementKey": token, "recordCount": count, "selected": None}
            for label, token, count in refs
        ],
    }


def price_dim(*refs: tuple[str, str, int]) -> dict:
    return {
        "label": "Price",
        "refinements": [
            {"label": label, "refinementKey": token, "recordCount": count, "selected": None}
            for label, token, count in refs
        ],
    }


class FakeClient:
    """Serves canned responses via a router(variables) callable."""

    def __init__(self, router):
        self._router = router
        self._count = 0

    async def post_graphql(self, variables):
        self._count += 1
        return self._router(variables)

    @property
    def is_throttled(self):
        return False

    @property
    def request_count(self):
        return self._count

    @property
    def failures(self):
        return {}

    async def close(self):
        pass


@pytest.fixture
def browse_settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        stores="2619",
        brands="Milwaukee",
        brand_tokens="Milwaukee:zv",
        root_nav_param="N-5yc1v",
        page_size=2,
        api_max_start_index=4,   # reachable cap = 6
        browse_network_categories_per_run=2,
        browse_cursor_path=str(tmp_path / "cursor.json"),
        keyword_pause_min_seconds=0,
        keyword_pause_max_seconds=0,
        store_raw_json=False,
    )


@pytest.fixture
def fresh_db(tmp_path):
    """Patch the module-level Database singleton so tests get an isolated DB."""
    from hd.db import base

    db = base.Database()
    with patch.object(base, "_default", db):
        yield db


# ---------------------------------------------------------------- parse_dimensions

class TestParseDimensions:
    def test_parses_labels_tokens_counts(self):
        raw = make_page([], 0, dims=[cat_dim(("Tools", "c1xy", 10), ("Plumbing", "bqew", 3))])
        dims = parse_dimensions(raw)
        assert dims["Category"] == [
            {"label": "Tools", "token": "c1xy", "count": 10},
            {"label": "Plumbing", "token": "bqew", "count": 3},
        ]

    def test_missing_dimensions_block(self):
        assert parse_dimensions({"data": {"searchModel": {}}}) == {}
        assert parse_dimensions({}) == {}
        assert parse_dimensions({"data": None}) == {}

    def test_null_refinements_and_missing_tokens_dropped(self):
        raw = {
            "data": {"searchModel": {"dimensions": [
                {"label": "Category", "refinements": None},
                {"label": None, "refinements": []},
                {"label": "Brand", "refinements": [
                    None,
                    {"label": "NoToken", "refinementKey": None, "recordCount": 5},
                    {"label": "Ok", "refinementKey": "zv", "recordCount": "7"},
                ]},
            ]}}
        }
        dims = parse_dimensions(raw)
        assert dims["Category"] == []
        assert dims["Brand"] == [{"label": "Ok", "token": "zv", "count": 7}]


# ---------------------------------------------------------------- planning

class TestPlanWalks:
    def test_build_nav(self):
        assert build_nav("N-5yc1v", "zv", "bqew") == "N-5yc1vZzvZbqew"
        assert build_nav("N-5yc1v") == "N-5yc1v"

    def test_under_cap_single_walk(self, browse_settings):
        walks, need = plan_walks("N-5yc1vZzv", "Milwaukee", 5, {}, browse_settings)
        assert walks == [Walk("N-5yc1vZzv", "Milwaukee", 5)]
        assert need == []

    def test_zero_or_none_total(self, browse_settings):
        assert plan_walks("N-5yc1vZzv", "x", 0, {}, browse_settings) == ([], [])
        assert plan_walks("N-5yc1vZzv", "x", None, {}, browse_settings) == ([], [])

    def test_over_cap_splits_by_category(self, browse_settings):
        dims = parse_dimensions(make_page([], 10, dims=[cat_dim(("Big", "big", 7), ("Small", "sm", 3))]))
        walks, need = plan_walks("N-5yc1vZzv", "Milwaukee", 10, dims, browse_settings)
        assert walks == [Walk("N-5yc1vZzvZsm", "Milwaukee/Small", 3)]
        assert need == [("N-5yc1vZzvZbig", "Milwaukee/Big")]

    def test_category_token_already_in_nav_skipped(self, browse_settings):
        dims = parse_dimensions(make_page([], 10, dims=[cat_dim(("Echo", "zv", 3), ("New", "nw", 3))]))
        walks, need = plan_walks("N-5yc1vZzv", "Milwaukee", 10, dims, browse_settings)
        assert [w.nav_param for w in walks] == ["N-5yc1vZzvZnw"]
        assert need == []

    def test_price_split_when_no_categories(self, browse_settings):
        dims = parse_dimensions(make_page([], 10, dims=[price_dim(("$0-$10", "p1", 4), ("$10+", "p2", 6))]))
        walks, need = plan_walks("N-5yc1vZzvZc", "Tools", 10, dims, browse_settings)
        assert [w.nav_param for w in walks] == ["N-5yc1vZzvZcZp1", "N-5yc1vZzvZcZp2"]
        assert not any(w.truncated for w in walks)
        assert need == []

    def test_no_facets_marks_truncated(self, browse_settings):
        walks, need = plan_walks("N-5yc1vZzv", "Milwaukee", 100, {}, browse_settings)
        assert len(walks) == 1 and walks[0].truncated
        assert need == []

    def test_depth_guard_marks_truncated(self, browse_settings):
        dims = parse_dimensions(make_page([], 100, dims=[cat_dim(("X", "x", 100))]))
        walks, need = plan_walks(
            "N-5yc1vZzv", "Milwaukee", 100, dims, browse_settings,
            depth=browse_settings.browse_max_split_depth,
        )
        assert len(walks) == 1 and walks[0].truncated


class TestResolveWalks:
    async def test_fetches_facets_for_oversized_children(self, browse_settings):
        """Brand set over cap → split by category; oversized category gets its own facet read."""
        def router(v):
            nav = v["navParam"]
            if nav == "N-5yc1vZzv":
                return make_page([], 10, dims=[cat_dim(("Big", "big", 8), ("Small", "sm", 2))])
            if nav == "N-5yc1vZzvZbig":
                return make_page([], 8, dims=[cat_dim(("A", "a", 5), ("B", "b", 3))])
            raise AssertionError(f"unexpected nav {nav}")

        client = FakeClient(router)
        walks = await resolve_walks(
            client, browse_settings, "N-5yc1vZzv", "Milwaukee", "2619", "IN_STORE",
        )
        navs = sorted(w.nav_param for w in walks)
        assert navs == ["N-5yc1vZzvZbigZa", "N-5yc1vZzvZbigZb", "N-5yc1vZzvZsm"]
        assert not any(w.truncated for w in walks)


# ---------------------------------------------------------------- walking

class TestWalkAndCapture:
    async def test_inserts_products_and_snapshots(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.db.models import Product, StoreSnapshot

        await base.init_db(browse_settings)

        pages = {
            0: make_page([make_product("111"), make_product("222", clearance={"value": 50.0, "dollarOff": 50.0, "percentageOff": 50})], 3),
            2: make_page([make_product("333"), make_product("999", brand="DEWALT")], 3),
            4: make_page([], 3),
        }

        def router(v):
            return pages[v["startIndex"]]

        client = FakeClient(router)
        seen: set[str] = set()
        walk = Walk("N-5yc1vZzv", "Milwaukee", 3)
        upserts, inserts = await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", seen, ["Milwaukee"],
        )

        assert upserts == 3          # DEWALT item filtered out
        assert inserts == 3
        assert seen == {"111", "222", "333"}

        async with base.get_session(browse_settings) as session:
            prods = (await session.execute(select(Product))).scalars().all()
            snaps = (await session.execute(select(StoreSnapshot))).scalars().all()
        assert {p.item_id for p in prods} == {"111", "222", "333"}
        by_item = {s.item_id: s for s in snaps}
        assert len(by_item) == 3
        assert float(by_item["222"].clearance_value) == 50.0
        assert by_item["222"].in_stock is True

    async def test_seen_items_deduped_across_walks(self, browse_settings, fresh_db):
        from hd.db import base

        await base.init_db(browse_settings)
        page = make_page([make_product("111")], 1)
        client = FakeClient(lambda v: page)
        seen: set[str] = set()
        walk = Walk("N-5yc1vZzv", "Milwaukee", 1)

        _, first = await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", seen, ["Milwaukee"])
        _, second = await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", seen, ["Milwaukee"])
        assert first == 1
        assert second == 0

    async def test_stops_at_start_index_ceiling(self, browse_settings, fresh_db):
        from hd.db import base

        await base.init_db(browse_settings)

        def router(v):
            assert v["startIndex"] <= browse_settings.api_max_start_index
            ids = [str(v["startIndex"]), str(v["startIndex"] + 1)]
            return make_page([make_product(i) for i in ids], 100)

        client = FakeClient(router)
        walk = Walk("N-5yc1vZzv", "Milwaukee", 100, truncated=True)
        upserts, _ = await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        # pages at startIndex 0, 2, 4 → 6 products, then ceiling stops the walk
        assert upserts == 6

    async def test_invalid_response_stops_walk(self, browse_settings, fresh_db):
        from hd.db import base

        await base.init_db(browse_settings)
        client = FakeClient(lambda v: {"errors": [{"message": "boom"}]})
        walk = Walk("N-5yc1vZzv", "Milwaukee", 10)
        upserts, inserts = await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert (upserts, inserts) == (0, 0)


# ---------------------------------------------------------------- run_browse integration

def full_router(store="2619"):
    """Shelf: 3 items at brand nav. Network: Garage/Plumbing/Tools categories, 2 per run."""
    shelf_pages = {
        0: make_page([make_product("111"), make_product("222")], 3),
        2: make_page([make_product("333", clearance={"value": 10.0, "dollarOff": 10.0, "percentageOff": 50})], 3),
    }
    net_pages = {
        "N-5yc1vZzvZgar": make_page([make_product("111"), make_product("444")], 2),
        "N-5yc1vZzvZbqew": make_page([make_product("555")], 1),
        "N-5yc1vZzvZc1xy": make_page([make_product("666")], 1),
    }

    def router(v):
        nav, sf, idx = v["navParam"], v["storefilter"], v["startIndex"]
        if v["pageSize"] == 1:  # facet read
            if sf == "IN_STORE":
                return make_page([], 3)
            return make_page([], 4, dims=[cat_dim(
                ("Garage", "gar", 2), ("Plumbing", "bqew", 1), ("Tools", "c1xy", 1),
            )])
        if sf == "IN_STORE":
            return shelf_pages[idx]
        return net_pages[nav]

    return router


class TestRunBrowse:
    async def test_two_tier_run_and_cursor_rotation(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.db.models import Product, StoreSnapshot
        from hd import rotation

        await base.init_db(browse_settings)

        summary = await run_browse(
            browse_settings, client=FakeClient(full_router()),
        )
        # shelf: 111,222,333; network run 1 walks Garage+Plumbing (sorted): new 444,555
        assert summary.snapshots == 5
        assert summary.aborted is False

        async with base.get_session(browse_settings) as session:
            prods = {p.item_id for p in (await session.execute(select(Product))).scalars().all()}
            snaps = [s.item_id for s in (await session.execute(select(StoreSnapshot))).scalars().all()]
        assert prods == {"111", "222", "333", "444", "555"}
        assert len(snaps) == 5  # 111 deduped between tiers

        cursors = rotation.load_cursors(browse_settings.browse_cursor_path)
        assert cursors["network|2619|zv"] == 2

        # Second run: network picks Tools, wraps to Garage → 666 is new
        summary2 = await run_browse(
            browse_settings, client=FakeClient(full_router()),
        )
        assert summary2.aborted is False
        async with base.get_session(browse_settings) as session:
            prods2 = {p.item_id for p in (await session.execute(select(Product))).scalars().all()}
        assert "666" in prods2
        cursors2 = rotation.load_cursors(browse_settings.browse_cursor_path)
        assert cursors2["network|2619|zv"] == 1  # advanced by 2, wrapped mod 3

    async def test_no_brand_tokens_is_safe(self, browse_settings, fresh_db):
        from hd.db import base

        await base.init_db(browse_settings)
        browse_settings.brand_tokens = ""
        summary = await run_browse(browse_settings, client=FakeClient(lambda v: make_page([], 0)))
        assert summary.snapshots == 0
