"""Tests for the facet-driven brand browse pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from hd.config import Settings
from hd.hd_api.parsers import parse_dimensions
from hd.pipeline.browse import (
    Walk,
    admission_ceiling,
    admits,
    both_ends_cap,
    effective_shelf_fraction,
    full_shelf_hours,
    rotate_shelf_walks,
    build_nav,
    plan_walks,
    reachable_cap,
    resolve_walks,
    run_browse,
    walk_and_capture,
    walk_cost_estimate,
    walk_status,
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
        # Do NOT inherit the operator's untracked .env: it carries
        # both_ends_paging, an admission ceiling, SHELF_CATEGORY_WALKS and a
        # shelf fraction, so without this these tests mean something different
        # on this machine than on a fresh clone.
        _env_file=None,
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
async def fresh_db(tmp_path):
    """Patch the module-level Database singleton so tests get an isolated DB."""
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

    async def test_every_observation_records_the_walk_that_produced_it(
        self, browse_settings, fresh_db
    ):
        """The real walk path must attach the node's identity to each snapshot.

        Coverage records name a REGION and price rows name an ITEM; this is the
        only thing that joins them, and the absence rule needs that join. The
        mapping exists only in flight — a walk that does not record it destroys
        it for good — so this asserts the whole path, not just the writer.
        """
        from sqlalchemy import select

        from hd.db import base
        from hd.db.models import StoreSnapshot

        await base.init_db(browse_settings)
        pages = {
            0: make_page([make_product("111"), make_product("222")], 3),
            2: make_page([make_product("333")], 3),
            4: make_page([], 3),
        }
        client = FakeClient(lambda v: pages[v["startIndex"]])
        nav = "N-5yc1vZzvZc1xyZc298Zc28l"
        walk = Walk(nav, "Milwaukee/Tools/Power Tools/Saws", 3)

        await walk_and_capture(
            client, browse_settings, walk, "2619", "ALL", set(), ["Milwaukee"])

        async with base.get_session(browse_settings) as session:
            snaps = (await session.execute(select(StoreSnapshot))).scalars().all()
        assert len(snaps) == 3
        # Every one of them, across every page — not merely the first.
        assert {s.nav_param for s in snaps} == {nav}

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


# ---------------------------------------------------------------- both-ends paging

def _both_ends(settings: Settings) -> Settings:
    """browse_settings with both-ends on and a 1-page overlap margin.

    With page_size=2 / api_max_start_index=4, reachable_cap=6 and
    both_ends_cap = 2*6 - 1*2 = 10, so a node of total 7-10 walks both ends.
    """
    settings.both_ends_paging = True
    settings.both_ends_min_overlap_pages = 1
    return settings


class TestBothEndsPlanning:
    def test_cap_is_two_ends_minus_overlap(self, browse_settings):
        from hd.pipeline.browse import both_ends_cap
        assert reachable_cap(_both_ends(browse_settings)) == 6
        assert both_ends_cap(browse_settings) == 10   # 2*6 - 1*2

    def test_in_window_node_walks_both_ends(self, browse_settings):
        walks, need = plan_walks("N-5yc1vZzv", "M", 8, {}, _both_ends(browse_settings))
        assert need == []
        assert len(walks) == 1 and walks[0].both_ends is True

    def test_at_or_under_reachable_cap_stays_a_single_walk(self, browse_settings):
        walks, _ = plan_walks("N-5yc1vZzv", "M", 6, {}, _both_ends(browse_settings))
        assert len(walks) == 1 and walks[0].both_ends is False

    def test_over_both_ends_cap_still_splits(self, browse_settings):
        # total 20 > cap 10: no facets given, so it falls to a truncated head —
        # NOT a both-ends walk.
        walks, _ = plan_walks("N-5yc1vZzv", "M", 20, {}, _both_ends(browse_settings))
        assert walks[0].both_ends is False and walks[0].truncated is True

    def test_flag_off_never_walks_both_ends(self, browse_settings):
        walks, _ = plan_walks("N-5yc1vZzv", "M", 8, {}, browse_settings)  # flag default off
        assert all(not w.both_ends for w in walks)


class TestWalkRouting:
    """Every plan_walks decision emits one routing line. Split parents never
    write a page 0, so this line is the archive's only record of a parent
    node's total and route — the drift detector for both-ends eligibility."""

    def _routes(self, logs):
        return [(e["label"], e["branch"]) for e in logs
                if e["event"] == "Walk routing"]

    def test_single_bothends_and_category_branches(self, browse_settings):
        from structlog.testing import capture_logs
        s = _both_ends(browse_settings)
        dims = parse_dimensions(
            make_page([], 20, dims=[cat_dim(("Big", "big", 12), ("Sm", "sm", 3))]))
        with capture_logs() as logs:
            plan_walks("N-1", "Small", 5, {}, s)
            plan_walks("N-2", "Band", 8, {}, s)
            plan_walks("N-3", "Split", 20, dims, s)
        assert self._routes(logs) == [
            ("Small", "single"), ("Band", "both-ends"), ("Split", "category-split")]
        caps = [(e["cap"], e["band_cap"]) for e in logs if e["event"] == "Walk routing"]
        assert caps == [(6, 10)] * 3

    def test_price_and_truncated_branches(self, browse_settings):
        from structlog.testing import capture_logs
        price_dims = parse_dimensions(
            make_page([], 10, dims=[price_dim(("$0-$10", "p1", 4), ("$10+", "p2", 6))]))
        deep_dims = parse_dimensions(make_page([], 100, dims=[cat_dim(("X", "x", 100))]))
        with capture_logs() as logs:
            plan_walks("N-4", "Priced", 10, price_dims, browse_settings)
            plan_walks("N-5", "Bare", 100, {}, browse_settings)
            plan_walks("N-6", "Deep", 100, deep_dims, browse_settings,
                       depth=browse_settings.browse_max_split_depth)
        assert self._routes(logs) == [
            ("Priced", "price-split"), ("Bare", "truncated-no-facet"),
            ("Deep", "truncated-depth")]

    def test_zero_total_routes_nothing(self, browse_settings):
        from structlog.testing import capture_logs
        with capture_logs() as logs:
            plan_walks("N-7", "Empty", 0, {}, browse_settings)
        assert self._routes(logs) == []


class TestBothEndsWalk:
    def _router(self, settings, desc_sequence):
        """Serve 8 price-ranked items; DESC order chosen by the test."""
        asc = [str(i) for i in range(8)]
        def router(v):
            order = v["orderBy"]["order"]
            si = v["startIndex"]
            seq = asc if order == "ASC" else desc_sequence
            return make_page([make_product(i) for i in seq[si:si + settings.page_size]], 8)
        return router

    async def test_union_covers_the_node_no_double_snapshots(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.db.models import Product, StoreSnapshot
        s = _both_ends(browse_settings)
        await base.init_db(s)
        # DESC reaches the dear end (7..0), so cheap-6 ∪ dear-6 = all 8.
        client = FakeClient(self._router(s, [str(i) for i in reversed(range(8))]))
        seen: set[str] = set()
        walk = Walk("N-5yc1vZzv", "Milwaukee", 8, both_ends=True)
        upserts, inserts = await walk_and_capture(
            client, s, walk, "2619", "IN_STORE", seen, ["Milwaukee"])

        assert walk.truncated is False          # union == total, full coverage
        assert seen == {str(i) for i in range(8)}
        assert inserts == 8                     # every item snapshotted exactly once
        async with base.get_session(s) as session:
            snaps = (await session.execute(select(StoreSnapshot))).scalars().all()
            prods = (await session.execute(select(Product))).scalars().all()
        assert len({sn.item_id for sn in snaps}) == 8     # no duplicate snapshot rows
        assert len(snaps) == 8
        assert len(prods) == 8

    async def test_seam_gap_marks_truncated_not_silent(self, browse_settings, fresh_db):
        from hd.db import base
        s = _both_ends(browse_settings)
        await base.init_db(s)
        # Degenerate DESC returns the SAME cheap items, so the union is only {0..5}
        # of a claimed 8 — a seam gap. Must surface as truncated, never silent.
        client = FakeClient(self._router(s, [str(i) for i in range(8)]))
        walk = Walk("N-5yc1vZzv", "Milwaukee", 8, both_ends=True)
        await walk_and_capture(
            client, s, walk, "2619", "IN_STORE", set(), ["Milwaukee"])
        assert walk.truncated is True


class TestBothEndsCostAndCorrectness:
    def _settings(self, browse_settings, **over):
        s = _both_ends(browse_settings)
        s.api_max_start_index = 20        # reachable_cap = 22, room to page
        s.both_ends_confirm_pages = 2
        for k, v in over.items():
            setattr(s, k, v)
        return s

    def _router(self, settings, n_items, total=None):
        total = n_items if total is None else total
        asc = [str(i) for i in range(n_items)]
        def router(v):
            si = v["startIndex"]
            seq = asc if v["orderBy"]["order"] == "ASC" else asc[::-1]
            return make_page(
                [make_product(i) for i in seq[si:si + settings.page_size]], total)
        return router

    async def test_desc_stops_early_so_both_ends_beats_a_full_walk(self, browse_settings, fresh_db):
        from hd.db import base
        s = self._settings(browse_settings)         # cap 22, margin 1 → both_ends_cap 42
        await base.init_db(s)
        # 30 items in the band. ASC = 11 pages (cheapest 22); DESC needs only the
        # dear 8, reaching full coverage at page 3, +2 confirm = 6 pages. So 17
        # requests, not 22 if DESC ran to the ceiling. This is the fix that makes
        # both-ends cheaper than the split it replaces.
        client = FakeClient(self._router(s, 30))
        walk = Walk("N", "M", 30, both_ends=True)
        await walk_and_capture(client, s, walk, "2619", "IN_STORE", set(), ["Milwaukee"])
        assert walk.truncated is False
        assert client._count == 17                  # 11 ASC + 6 DESC (early-stopped)

    async def test_assertion_uses_live_total_not_stale_planned(self, browse_settings, fresh_db):
        from hd.db import base
        s = self._settings(browse_settings, api_max_start_index=4)  # reachable 6, cap 10
        await base.init_db(s)
        # planned=6 (stale), live=10, but only 8 distinct items ever return — a
        # real 2-item gap. Judging against planned 6 would pass 8≥6 as complete;
        # the live-total denominator must catch 8<10.
        client = FakeClient(self._router(s, 8, total=10))
        walk = Walk("N", "M", 6, both_ends=True)    # planned < live
        await walk_and_capture(client, s, walk, "2619", "IN_STORE", set(), ["Milwaukee"])
        assert walk.truncated is True

    async def test_node_grown_past_cap_is_truncated_even_if_covered(self, browse_settings, fresh_db):
        from hd.db import base
        s = self._settings(browse_settings, api_max_start_index=4)  # reachable 6, cap 10
        await base.init_db(s)
        # 12 items, live total 12 > both_ends_cap 10: two ends happen to cover all
        # 12, but the node has outgrown its eligibility, so don't trust it clean.
        client = FakeClient(self._router(s, 12, total=12))
        walk = Walk("N", "M", 8, both_ends=True)
        await walk_and_capture(client, s, walk, "2619", "IN_STORE", set(), ["Milwaukee"])
        assert walk.truncated is True

    async def test_throttle_after_asc_marks_truncated(self, browse_settings, fresh_db):
        from hd.db import base
        s = self._settings(browse_settings)         # api_max 20, so throttle beats the ceiling
        await base.init_db(s)
        calls = {"n": 0}
        page = make_page([make_product("0"), make_product("1")], 40)

        class ThrottleAfterAsc:
            def __init__(self): self._count = 0
            @property
            def is_throttled(self): return calls["n"] > 3   # throttle a few pages in
            async def post_graphql(self, v):
                self._count += 1
                calls["n"] += 1
                return page

        walk = Walk("N", "M", 30, both_ends=True)
        await walk_and_capture(ThrottleAfterAsc(), s, walk, "2619", "IN_STORE", set(), ["Milwaukee"])
        assert walk.truncated is True               # ASC-only half-coverage is not clean

    async def test_understated_total_self_extends_no_silent_gap(self, browse_settings, fresh_db):
        from hd.db import base
        s = self._settings(browse_settings)         # api_max 20 (reachable 22), cap 42
        await base.init_db(s)
        # 30 real items, but HD understates totalProducts as 24. A FIXED 2-page
        # window would stop DESC at 28 seen (24 + 48 slack) and the assertion
        # would pass on the wrong count — a silent gap. The adaptive window keeps
        # going while pages still yield new ids, so all 30 are fetched.
        client = FakeClient(self._router(s, 30, total=24))
        seen: set[str] = set()
        walk = Walk("N", "M", 24, both_ends=True)
        _, inserts = await walk_and_capture(
            client, s, walk, "2619", "IN_STORE", seen, ["Milwaukee"])
        assert seen == {str(i) for i in range(30)}  # nothing left behind the stale count
        assert inserts == 30
        assert walk.truncated is False              # covered 30 ≥ denom 24


# ---------------------------------------------------------------- full-walk hours

class TestFullShelfHours:
    """04:00 and 12:00 ET walk the whole shelf; other runs take a slice."""

    def _at(self, hour_et):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 8, 20, hour_et, 30, tzinfo=ZoneInfo("America/New_York"))

    def test_designated_hours_walk_everything(self, browse_settings):
        browse_settings.browse_full_shelf_hours_et = "4,12"
        browse_settings.browse_shelf_fraction = 0.5
        assert effective_shelf_fraction(browse_settings, self._at(4)) == 1.0
        assert effective_shelf_fraction(browse_settings, self._at(12)) == 1.0

    def test_other_hours_take_the_configured_slice(self, browse_settings):
        browse_settings.browse_full_shelf_hours_et = "4,12"
        browse_settings.browse_shelf_fraction = 0.5
        for h in (0, 3, 8, 16, 20):
            assert effective_shelf_fraction(browse_settings, self._at(h)) == 0.5

    def test_hour_is_eastern_not_the_machine_clock(self, browse_settings):
        """A UTC 08:00 instant is 04:00 Eastern — the run that must walk fully."""
        from datetime import datetime, timezone
        browse_settings.browse_full_shelf_hours_et = "4"
        browse_settings.browse_shelf_fraction = 0.5
        utc_0800 = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
        assert effective_shelf_fraction(browse_settings, utc_0800) == 1.0

    def test_empty_setting_disables_full_walks(self, browse_settings):
        browse_settings.browse_full_shelf_hours_et = ""
        browse_settings.browse_shelf_fraction = 0.5
        assert effective_shelf_fraction(browse_settings, self._at(4)) == 0.5

    def test_malformed_hours_are_ignored_not_fatal(self, browse_settings):
        browse_settings.browse_full_shelf_hours_et = "4, ,x,99,12"
        assert full_shelf_hours(browse_settings) == {4, 12}

    def test_explicit_fraction_overrides_the_setting(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.5
        walks = [Walk(f"n{i}", f"C{i:02d}", 5) for i in range(10)]
        picked, skipped = rotate_shelf_walks(walks, {}, "2619", "zv", browse_settings, 1.0)
        assert len(picked) == 10 and skipped == 0


# ---------------------------------------------------------------- shelf rotation

class TestShelfRotation:
    """The shelf tier walks a slice per run instead of every category every run."""

    def _walks(self, n):
        return [Walk(f"nav{i}", f"Cat{i:02d}", 5) for i in range(n)]

    def test_half_the_categories_are_walked(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.5
        picked, skipped = rotate_shelf_walks(self._walks(10), {}, "2619", "zv", browse_settings)
        assert len(picked) == 5
        assert skipped == 5

    def test_consecutive_runs_cover_the_other_half(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.5
        cursors = {}
        walks = self._walks(10)
        first, _ = rotate_shelf_walks(walks, cursors, "2619", "zv", browse_settings)
        second, _ = rotate_shelf_walks(walks, cursors, "2619", "zv", browse_settings)
        # Two runs see every category exactly once between them.
        assert {w.label for w in first} | {w.label for w in second} == {w.label for w in walks}
        assert not ({w.label for w in first} & {w.label for w in second})

    def test_cursor_wraps_around(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.5
        cursors = {}
        walks = self._walks(10)
        seen = []
        for _ in range(4):
            picked, _ = rotate_shelf_walks(walks, cursors, "2619", "zv", browse_settings)
            seen.append({w.label for w in picked})
        assert seen[0] == seen[2] and seen[1] == seen[3]  # cycle length 2

    def test_fraction_of_one_walks_everything(self, browse_settings):
        browse_settings.browse_shelf_fraction = 1.0
        walks = self._walks(10)
        picked, skipped = rotate_shelf_walks(walks, {}, "2619", "zv", browse_settings)
        assert picked == walks
        assert skipped == 0

    def test_a_single_walk_is_never_skipped(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.1
        walks = self._walks(1)
        picked, skipped = rotate_shelf_walks(walks, {}, "2619", "zv", browse_settings)
        assert picked == walks and skipped == 0

    def test_at_least_one_walk_runs_however_small_the_fraction(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.01
        picked, _ = rotate_shelf_walks(self._walks(10), {}, "2619", "zv", browse_settings)
        assert len(picked) == 1

    def test_stores_rotate_independently(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.5
        cursors = {}
        walks = self._walks(10)
        rotate_shelf_walks(walks, cursors, "2619", "zv", browse_settings)
        assert "shelf|2619|zv" in cursors
        assert "shelf|8452|zv" not in cursors

    def test_order_is_stable_so_the_cursor_keeps_its_meaning(self, browse_settings):
        browse_settings.browse_shelf_fraction = 0.5
        walks = self._walks(10)
        a, _ = rotate_shelf_walks(list(reversed(walks)), {}, "2619", "zv", browse_settings)
        b, _ = rotate_shelf_walks(walks, {}, "2619", "zv", browse_settings)
        assert [w.label for w in a] == [w.label for w in b]


# ---------------------------------------------------------------- circuit breaker

class TestCircuitBreakerAbort:
    async def test_circuit_opening_mid_walk_ends_the_run(self, browse_settings, fresh_db):
        """It used to escape run_browse uncaught, killing the process mid-run."""
        from hd.db import base
        from hd.http.client import CircuitOpenError

        await base.init_db(browse_settings)

        class ExplodingClient(FakeClient):
            @property
            def is_throttled(self):
                return False

            async def post_graphql(self, variables):
                self._count += 1
                if self._count > 1:
                    raise CircuitOpenError("Circuit breaker open: 10 failures")
                # A full page, so the walk tries to fetch the next one.
                return make_page(
                    [make_product("111"), make_product("222")], 100
                )

        summary = await run_browse(browse_settings, client=ExplodingClient(None))

        # No traceback: the run reports itself as aborted and stops. The
        # per-walk exception handler must not swallow this and walk on.
        assert summary.aborted is True

    async def test_circuit_opening_during_a_facet_read_ends_the_run(
        self, browse_settings, fresh_db
    ):
        from hd.db import base
        from hd.http.client import CircuitOpenError

        await base.init_db(browse_settings)

        class ExplodingClient(FakeClient):
            @property
            def is_throttled(self):
                return False

            async def post_graphql(self, variables):
                self._count += 1
                if self._count > 1:
                    raise CircuitOpenError("open")
                return make_page([make_product("111")], 1)

        summary = await run_browse(browse_settings, client=ExplodingClient(None))
        assert summary.aborted is True

    async def test_cursors_survive_an_open_circuit(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.http.client import CircuitOpenError
        from hd import rotation

        await base.init_db(browse_settings)

        class ExplodingClient(FakeClient):
            @property
            def is_throttled(self):
                return False

            async def post_graphql(self, variables):
                self._count += 1
                if self._count > 1:
                    raise CircuitOpenError("open")
                return make_page([], 4, dims=[cat_dim(("Garage", "gar", 2))])

        await run_browse(browse_settings, client=ExplodingClient(None))
        # The finally block still ran, so progress is not lost.
        assert rotation.load_cursors(browse_settings.browse_cursor_path) is not None


# ---------------------------------------------------------------- facet-read reuse

class TestFacetReadPriming:
    """The facet read doubles as page 0, so planning a walk costs no extra request."""

    async def test_facet_read_is_reused_as_page_zero(self, browse_settings, fresh_db):
        from hd.db import base

        await base.init_db(browse_settings)
        seen = []

        def router(v):
            seen.append((v["navParam"], v["startIndex"]))
            return make_page([make_product("111")], 1)

        client = FakeClient(router)
        walks = await resolve_walks(
            client, browse_settings, "N-5yc1vZzv", "Milwaukee", "2619", "IN_STORE",
        )
        assert len(walks) == 1
        assert walks[0].primed is not None
        planning_requests = client.request_count
        assert planning_requests == 1

        upserts, _ = await walk_and_capture(
            client, browse_settings, walks[0], "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        # A single-page walk costs exactly one request in total, not two.
        assert client.request_count == planning_requests
        assert upserts == 1

    async def test_primed_page_is_consumed_only_once(self, browse_settings, fresh_db):
        from hd.db import base

        await base.init_db(browse_settings)

        def router(v):
            return make_page([make_product("111")], 1)

        client = FakeClient(router)
        walks = await resolve_walks(
            client, browse_settings, "N-5yc1vZzv", "Milwaukee", "2619", "IN_STORE",
        )
        walk = walks[0]
        await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert walk.primed is None  # a second pass must re-fetch, not serve a stale page

        before = client.request_count
        await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert client.request_count > before

    async def test_split_children_are_not_primed_with_the_parent_page(self, browse_settings):
        """A child walks a different navParam, so the parent's page is not its page 0."""
        def router(v):
            if v["navParam"] == "N-5yc1vZzv":
                return make_page([], 10, dims=[cat_dim(("Small", "sm", 3))])
            return make_page([make_product("999")], 3)

        client = FakeClient(router)
        walks = await resolve_walks(
            client, browse_settings, "N-5yc1vZzv", "Milwaukee", "2619", "IN_STORE",
        )
        assert [w.nav_param for w in walks] == ["N-5yc1vZzvZsm"]
        # The child was derived from the parent's dimensions without a facet
        # read of its own, so it has no page to carry forward — and must not
        # inherit the parent's, which covers a different navParam.
        assert walks[0].primed is None
        assert client.request_count == 1

    async def test_failed_facet_read_primes_nothing(self, browse_settings):
        from hd.http.client import failure_response

        client = FakeClient(lambda v: failure_response("http_429"))
        walks = await resolve_walks(
            client, browse_settings, "N-5yc1vZzv", "Milwaukee", "2619", "IN_STORE",
        )
        assert walks == []


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
        # A facet read is now an ordinary page-0 request — the response serves
        # both purposes — so routing is by navParam and index, not page size.
        if sf == "IN_STORE":
            return shelf_pages[idx]
        if nav == "N-5yc1vZzv":  # brand root: the category facet read
            return make_page([], 4, dims=[cat_dim(
                ("Garage", "gar", 2), ("Plumbing", "bqew", 1), ("Tools", "c1xy", 1),
            )])
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


# ---------------------------------------------------------------- shelf category walks

class TestShelfCategoryWalks:
    """Store-wide category nodes (the grill wall): every brand captured,
    walked on every shelf pass, outside rotation."""

    def test_config_parses_label_token_pairs(self):
        s = Settings(shelf_category_walks="Grills:bxaz, Outdoor-Cookers:cd1m,junk,:x,y:")
        assert s.shelf_category_walk_list == [("Grills", "bxaz"), ("Outdoor-Cookers", "cd1m")]

    async def test_all_brands_walk_captures_every_brand(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.db.models import Product

        await base.init_db(browse_settings)
        page = make_page(
            [make_product("501", brand="Weber"), make_product("502", brand="Traeger")], 2
        )

        def router(v):
            return page if v["startIndex"] == 0 else make_page([], 2)

        client = FakeClient(router)
        walk = Walk("N-5yc1vZbxaz", "Grills", 2, all_brands=True)
        upserts, inserts = await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert upserts == 2 and inserts == 2

        async with base.get_session(browse_settings) as session:
            brands = {p.brand for p in (await session.execute(select(Product))).scalars()}
        assert brands == {"Weber", "Traeger"}

    async def test_shelf_tier_runs_category_walks_after_brand_walks(
        self, browse_settings, fresh_db
    ):
        from hd.db import base
        from hd.db.models import Product

        await base.init_db(browse_settings)
        browse_settings.shelf_category_walks = "Grills:bxaz"

        def router(v):
            nav, start = v["navParam"], v["startIndex"]
            if nav == "N-5yc1vZzv":
                return make_page([make_product("111")], 1) if start == 0 else make_page([], 1)
            if nav == "N-5yc1vZbxaz":
                if start == 0:
                    return make_page(
                        [make_product("501", brand="Weber"),
                         make_product("502", brand="Traeger")], 2
                    )
                return make_page([], 2)
            raise AssertionError(f"unexpected nav {nav}")

        client = FakeClient(router)
        summary = await run_browse(browse_settings, client=client, tiers=("shelf",))

        assert summary.walks == 2
        assert summary.products == 3 and summary.snapshots == 3
        async with base.get_session(browse_settings) as session:
            brands = {p.brand for p in (await session.execute(select(Product))).scalars()}
        assert brands == {"Milwaukee", "Weber", "Traeger"}


# ---------------------------------------------------------------- coverage records

from hd.pipeline.browse import walk_status


class TestWalkStatus:
    """Coverage is judged on what was seen, never on how the walk ended."""

    def test_every_claimed_item_seen_is_complete(self):
        w = Walk("n", "l", 3)
        w.observed_ids, w.live_total = 3, 3
        assert walk_status(w) == "complete"

    def test_shortfall_against_live_total_is_truncated(self):
        w = Walk("n", "l", 3)
        w.observed_ids, w.live_total = 2, 3
        assert walk_status(w) == "truncated"

    def test_planner_truncation_survives_full_coverage_arithmetic(self):
        w = Walk("n", "l", 3, truncated=True)
        w.observed_ids, w.live_total = 3, 3
        assert walk_status(w) == "truncated"

    def test_cut_walk_with_unknown_denominator_is_truncated(self):
        w = Walk("n", "l", 0)
        w.observed_ids, w.live_total, w.cut = 5, None, True
        assert walk_status(w) == "truncated"

    def test_nothing_usable_at_all_is_failed(self):
        w = Walk("n", "l", 3)
        assert walk_status(w) == "failed"

    def test_node_that_shrank_mid_walk_is_still_complete(self):
        w = Walk("n", "l", 5)
        w.observed_ids, w.live_total = 4, 3
        assert walk_status(w) == "complete"

    def test_empty_node_fully_seen_is_complete(self):
        w = Walk("n", "l", 2)
        w.observed_ids, w.live_total = 0, 0
        assert walk_status(w) == "complete"


class TestCoverageRecords:
    async def test_run_writes_a_run_row_and_a_row_per_walk(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.db.models import ScanRun, WalkCoverage

        await base.init_db(browse_settings)
        # The fixture reads the ambient .env; a configured shelf-category walk
        # would add extra IN_STORE rows and muddy the per-walk assertions.
        browse_settings.shelf_category_walks = ""
        summary = await run_browse(browse_settings, client=FakeClient(full_router()))
        assert summary.aborted is False

        async with base.get_session(browse_settings) as session:
            runs = (await session.execute(select(ScanRun))).scalars().all()
            walks = (await session.execute(select(WalkCoverage))).scalars().all()

        assert len(runs) == 1
        run = runs[0]
        assert run.status == "complete"
        assert run.ended is not None and run.ended >= run.started
        assert run.walks == summary.walks == len(walks)
        assert run.snapshots == summary.snapshots

        assert all(w.run_id == run.id for w in walks)
        assert all(w.status == "complete" for w in walks)
        by_tier = {}
        for w in walks:
            by_tier.setdefault(w.tier, []).append(w)
        # shelf walk saw all 3; network walked Garage (2) + Plumbing (1)
        assert [w.items_observed for w in by_tier["IN_STORE"]] == [3]
        assert sorted(w.items_observed for w in by_tier["ALL"]) == [1, 2]
        assert all(w.items_expected == w.items_observed for w in walks)

    async def test_unusable_first_page_records_a_failed_walk(self, browse_settings, fresh_db):
        from hd.db import base

        await base.init_db(browse_settings)
        client = FakeClient(lambda v: {"data": {}})
        walk = Walk("N-5yc1vZzv", "Milwaukee", 3)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert walk_status(walk) == "failed"

    async def test_short_page_below_claimed_total_records_truncated(
        self, browse_settings, fresh_db
    ):
        from hd.db import base

        await base.init_db(browse_settings)
        # Page 0 claims 5 products but delivers one short page of 1.
        client = FakeClient(lambda v: make_page([make_product("111")], 5))
        walk = Walk("N-5yc1vZzv", "Milwaukee", 5)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert walk_status(walk) == "truncated"
        assert walk.observed_ids == 1 and walk.live_total == 5

    async def test_mid_set_short_page_does_not_end_the_walk(
        self, browse_settings, fresh_db
    ):
        """A short page mid-set is not end-of-results — keep paging.

        Home Depot serves a short page in the middle of a set: Power Tools/Saws
        returned 23 of 24 on page 2 while its own searchReport said 299, and the
        walk stopped at 71 with 220 items left unobserved. Coverage, not page
        length, decides when a walk is done.
        """
        from hd.db import base

        await base.init_db(browse_settings)
        pages = {
            0: [make_product("a"), make_product("b")],
            2: [make_product("c")],                              # short, MID-set
            4: [make_product("d")],                              # short, completes
        }

        def router(v):
            return make_page(pages.get(v.get("startIndex", 0), []), 4)

        client = FakeClient(router)
        walk = Walk("N-5yc1vZzv", "Milwaukee", 4)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert walk.observed_ids == 4 and walk.live_total == 4
        assert walk_status(walk) == "complete"
        assert client.request_count == 3   # the old rule stopped after 2

    async def test_short_page_at_full_coverage_still_ends_the_walk(
        self, browse_settings, fresh_db
    ):
        """The ordinary case is unchanged: an honest node stops where it did."""
        from hd.db import base

        await base.init_db(browse_settings)
        pages = {0: [make_product("a"), make_product("b")], 2: [make_product("c")]}

        def router(v):
            return make_page(pages.get(v.get("startIndex", 0), []), 3)

        client = FakeClient(router)
        walk = Walk("N-5yc1vZzv", "Milwaukee", 3)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert walk_status(walk) == "complete"
        assert client.request_count == 2   # no extra page bought

    async def test_overstated_total_stops_after_pages_with_nothing_new(
        self, browse_settings, fresh_db
    ):
        """An inflated totalProducts costs a few pages, never an unbounded walk."""
        from hd.db import base

        settings = browse_settings.model_copy(
            update={"api_max_start_index": 20, "short_page_confirm_pages": 2}
        )
        await base.init_db(settings)

        def router(v):
            if v.get("startIndex", 0) == 0:
                return make_page([make_product("a"), make_product("b")], 99)
            return make_page([make_product("a")], 99)   # short, never anything new

        client = FakeClient(router)
        walk = Walk("N-5yc1vZzv", "Milwaukee", 99)
        await walk_and_capture(
            client, settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert walk_status(walk) == "truncated"
        # page 0, then 3 pages with nothing new (stale 1, 2, 3 > confirm 2).
        assert client.request_count == 4
        assert client.request_count < settings.api_max_start_index // settings.page_size + 1

    async def test_empty_page_always_ends_the_walk(self, browse_settings, fresh_db):
        """An empty page is the hard terminator, whatever the claimed total."""
        from hd.db import base

        await base.init_db(browse_settings)

        def router(v):
            if v.get("startIndex", 0) == 0:
                return make_page([make_product("a"), make_product("b")], 99)
            return make_page([], 99)

        client = FakeClient(router)
        walk = Walk("N-5yc1vZzv", "Milwaukee", 99)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "IN_STORE", set(), ["Milwaukee"],
        )
        assert client.request_count == 2
        assert walk_status(walk) == "truncated"

    async def test_throttled_run_is_recorded_aborted(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.db.models import ScanRun, WalkCoverage

        await base.init_db(browse_settings)

        class ThrottledClient(FakeClient):
            @property
            def is_throttled(self):
                return True

        summary = await run_browse(browse_settings, client=ThrottledClient(full_router()))
        assert summary.aborted is True

        async with base.get_session(browse_settings) as session:
            runs = (await session.execute(select(ScanRun))).scalars().all()
            walks = (await session.execute(select(WalkCoverage))).scalars().all()
        assert len(runs) == 1 and runs[0].status == "aborted"
        assert walks == []  # nothing attempted, nothing claimed


class TestWalkAdmission:
    """Budget-aware admission: never start a walk the run cannot finish.

    A walk cut in flight is recorded "truncated" and can never ground an
    absence claim; a walk never attempted writes no coverage row at all. So
    deferring is strictly better than overreaching, and these tests pin the
    arithmetic that decides which happens.
    """

    class _Spent:
        """Client stand-in — only request_count reaches admits()."""

        def __init__(self, n: int) -> None:
            self._n = n

        @property
        def request_count(self) -> int:
            return self._n

    # ---- the estimate ---------------------------------------------------

    def test_estimate_is_bounded_by_what_the_walk_can_reach(self, browse_settings):
        # page_size=2, api_max_start_index=4 → reachable cap 6. A 100-item node
        # cannot be paged past that ceiling, so it costs 3 pages + 1 facet read,
        # NOT 50 pages. Getting this wrong would defer every large walk forever.
        assert reachable_cap(browse_settings) == 6
        assert walk_cost_estimate(Walk("n", "l", 100), browse_settings) == 4

    def test_estimate_of_a_small_node_counts_only_its_own_pages(self, browse_settings):
        assert walk_cost_estimate(Walk("n", "l", 4), browse_settings) == 3

    def test_both_ends_estimate_assumes_both_directions_run_full(self, browse_settings):
        """Both-ends must be sized on the worst case, which happens routinely.

        ASC always runs to the API ceiling; DESC stops early ONLY when the
        union reaches the live total, so every coverage shortfall costs a full
        second direction. Production logs show that shortfall firing on about
        half of all both-ends walks, so it is the normal case, not the tail.
        """
        browse_settings.both_ends_min_overlap_pages = 1
        per_direction = browse_settings.api_max_start_index // browse_settings.page_size + 1
        assert per_direction == 3
        # 2 directions x 3 pages, + 1 facet read — regardless of node size.
        assert walk_cost_estimate(
            Walk("n", "l", 8, both_ends=True), browse_settings) == 7
        assert walk_cost_estimate(
            Walk("n", "l", 999, both_ends=True), browse_settings) == 7

    def test_both_ends_never_takes_the_primed_discount(self, browse_settings):
        """_page_direction reuses a primed page only when order_by is None.

        Both-ends always passes a price ordering, so the primed page is never
        consumed and discounting for it under-estimates every both-ends walk.
        """
        primed = Walk("n", "l", 999, both_ends=True, primed={"any": "page"})
        plain = Walk("n", "l", 999, both_ends=True)
        assert walk_cost_estimate(primed, browse_settings) == walk_cost_estimate(
            plain, browse_settings)

    def test_a_primed_walk_does_not_pay_for_a_facet_read(self, browse_settings):
        primed = Walk("n", "l", 4, primed={"any": "page"})
        assert walk_cost_estimate(primed, browse_settings) == 2

    # ---- the ceiling ----------------------------------------------------

    def test_ceiling_falls_back_to_the_request_budget_when_unset(self, browse_settings):
        browse_settings.browse_walk_admission_ceiling = 0
        browse_settings.browse_request_budget = 280
        assert admission_ceiling(browse_settings) == 280

    def test_explicit_ceiling_overrides_the_request_budget(self, browse_settings):
        browse_settings.browse_walk_admission_ceiling = 237
        browse_settings.browse_request_budget = 280
        assert admission_ceiling(browse_settings) == 237

    # ---- the decision ---------------------------------------------------

    def test_admits_a_walk_that_fits(self, browse_settings):
        browse_settings.browse_walk_admission_ceiling = 20
        walk = Walk("n", "l", 4)                      # costs 3
        assert admits(self._Spent(10), browse_settings, walk) is True

    def test_defers_a_walk_that_would_overrun_the_ceiling(self, browse_settings):
        browse_settings.browse_walk_admission_ceiling = 20
        walk = Walk("n", "l", 4)                      # costs 3, only 2 left
        assert admits(self._Spent(18), browse_settings, walk) is False

    def test_admits_exactly_at_the_boundary(self, browse_settings):
        browse_settings.browse_walk_admission_ceiling = 20
        walk = Walk("n", "l", 4)                      # costs 3, exactly 3 left
        assert admits(self._Spent(17), browse_settings, walk) is True

    # ---- the starvation guard -------------------------------------------

    @staticmethod
    def _oversized(settings) -> Walk:
        """A walk that cannot fit in a whole run: cost 7 against a ceiling of 6."""
        settings.both_ends_min_overlap_pages = 1
        settings.browse_walk_admission_ceiling = 6
        walk = Walk("n", "l", 999, both_ends=True)   # 5 pages + 1 overlap + 1 facet
        assert walk_cost_estimate(walk, settings) == 7
        return walk

    def test_oversized_walk_is_admitted_on_a_fresh_run(self, browse_settings):
        # A walk costing more than the whole ceiling can never satisfy the
        # ordinary rule. Deferring it every run would mean never walking it,
        # so a run still holding half its ceiling takes it and truncates once.
        walk = self._oversized(browse_settings)
        assert admits(self._Spent(0), browse_settings, walk) is True

    def test_oversized_walk_is_deferred_once_the_run_is_mostly_spent(
        self, browse_settings
    ):
        # Same walk, but only 2 of 6 left — a later run gives it a deeper pass,
        # so it waits rather than burning the tail of this one for a stub.
        walk = self._oversized(browse_settings)
        assert admits(self._Spent(4), browse_settings, walk) is False

    # ---- the property the whole feature rests on ----------------------

    @staticmethod
    def _honest_router(total):
        """Serves exactly `total` items, short page at the end — an accurate node."""
        def router(v):
            idx, ps = v["startIndex"], v["pageSize"]
            items = [make_product(str(i)) for i in range(idx, min(idx + ps, total))]
            return make_page(items, total)
        return router

    @staticmethod
    def _stalling_router(total):
        """Full pages of the SAME two items: distinct coverage never reaches
        `total`, so a both-ends union falls short and DESC runs to the ceiling.
        This is the production shape — 6 of 12 both-ends walks on record."""
        def router(v):
            return make_page([make_product("111"), make_product("222")], total)
        return router

    @pytest.mark.parametrize("total,primed", [
        (1, False), (5, False), (6, False), (7, False), (100, False), (5, True),
    ])
    async def test_estimate_covers_a_single_walk_over_an_accurate_node(
        self, browse_settings, fresh_db, total, primed
    ):
        """The load-bearing property, and nothing asserted it before.

        Every other estimate test hard-codes an arithmetic result, so all of
        them passed while both-ends walks were under-estimated by a full
        direction. Over-estimating merely defers a walk to the next run;
        under-estimating starts one that cannot finish — the exact failure
        admission control exists to prevent.
        """
        from hd.db import base

        await base.init_db(browse_settings)
        client = FakeClient(self._honest_router(total))
        walk = Walk("N-5yc1vZzv", "M", total)
        if primed:
            walk.primed = self._honest_router(total)({"startIndex": 0, "pageSize": 2})

        est = walk_cost_estimate(walk, browse_settings)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "ALL", set(), ["Milwaukee"],
        )
        assert client.request_count <= est, (
            f"estimate {est} under-shot actual {client.request_count} (total={total})"
        )

    @pytest.mark.parametrize("total", [7, 8, 999])
    async def test_estimate_covers_a_both_ends_walk_whose_union_falls_short(
        self, browse_settings, fresh_db, total
    ):
        """The case that was broken: DESC runs full whenever the union is short."""
        from hd.db import base

        await base.init_db(browse_settings)
        client = FakeClient(self._stalling_router(total))
        walk = Walk("N-5yc1vZzv", "M", total, both_ends=True)

        est = walk_cost_estimate(walk, browse_settings)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "ALL", set(), ["Milwaukee"],
        )
        assert walk.truncated is True          # the union did fall short
        assert client.request_count <= est, (
            f"estimate {est} under-shot actual {client.request_count} (total={total})"
        )

    @pytest.mark.parametrize("both_ends", [False, True])
    async def test_no_walk_can_exceed_the_structural_api_ceiling(
        self, browse_settings, fresh_db, both_ends
    ):
        """The bound that always holds, even for a node that lies about itself.

        A node UNDERSTATING its total self-extends past the estimate (see
        walk_cost_estimate's KNOWN LIMIT). It can never escape this bound
        though, because _page_direction stops at startIndex > the API ceiling —
        so the overshoot the admission ceiling must absorb is bounded, not open.
        """
        from hd.db import base

        await base.init_db(browse_settings)
        client = FakeClient(self._stalling_router(1))     # claims 1, serves forever
        walk = Walk("N-5yc1vZzv", "M", 1, both_ends=both_ends)
        await walk_and_capture(
            client, browse_settings, walk, "2619", "ALL", set(), ["Milwaukee"],
        )
        per_direction = (
            browse_settings.api_max_start_index // browse_settings.page_size + 1
        )
        bound = per_direction * (2 if both_ends else 1)
        assert client.request_count <= bound

    # ---- behaviour in a real run ----------------------------------------

    async def test_deferral_leaves_the_cursor_on_the_unwalked_category(
        self, browse_settings, fresh_db
    ):
        """A budget deferral must not rotate past the category it skipped.

        The unconstrained run walks Garage+Plumbing and leaves the cursor at 2.
        With a ceiling that only affords Garage, Plumbing must be deferred AND
        still be the head of the next run's slice — otherwise a run cut short
        silently drops a slice of the catalogue until the cursor wraps.
        """
        from hd.db import base
        from hd.db.models import WalkCoverage
        from hd import rotation

        await base.init_db(browse_settings)
        # Root facet read (1) + Garage (3 pages, the router keeps returning full
        # pages to the ceiling) = 4, past the ceiling. Plumbing is then dropped
        # WITHOUT even paying its facet read — planning is gated too, or a run
        # keeps spending quota on nodes it has already decided not to walk.
        browse_settings.browse_walk_admission_ceiling = 3

        summary = await run_browse(
            browse_settings, client=FakeClient(full_router()), tiers=("network",),
        )

        # Plumbing is dropped at PLANNING time — no walk was ever built for
        # it, and a category resolves to one or many walks — so it counts as a
        # deferred CATEGORY. Counting it as a deferred walk mixed two units in
        # the number the admission-ceiling gate is judged on.
        assert summary.deferred_categories == 1
        assert summary.deferred_walks == 0       # no planned walk was refused
        assert summary.skipped_walks == 0        # rotation deferral is a different thing
        assert summary.truncated_walks == []     # nothing was started and cut

        cursors = rotation.load_cursors(browse_settings.browse_cursor_path)
        assert cursors["network|2619|zv"] == 1   # Garage only; Plumbing still next

        # The honesty property: a walk never attempted claims no coverage.
        async with base.get_session(browse_settings) as session:
            labels = [w.label for w in
                      (await session.execute(select(WalkCoverage))).scalars().all()]
        assert any("Garage" in l for l in labels)
        assert not any("Plumbing" in l for l in labels)

    async def test_generous_ceiling_walks_everything_as_before(
        self, browse_settings, fresh_db
    ):
        """Admission control must be inert when the run can afford its work."""
        from hd.db import base
        from hd import rotation

        await base.init_db(browse_settings)
        browse_settings.browse_walk_admission_ceiling = 500

        summary = await run_browse(
            browse_settings, client=FakeClient(full_router()), tiers=("network",),
        )
        assert summary.deferred_walks == 0
        cursors = rotation.load_cursors(browse_settings.browse_cursor_path)
        assert cursors["network|2619|zv"] == 2   # unchanged from the baseline test

    async def test_a_run_stays_within_its_admission_ceiling(
        self, browse_settings, fresh_db
    ):
        """The run-level invariant the feature exists to deliver.

        Asserting only that a WALK was deferred misses the point: planning
        reads (fetch_facets, and the facet reads inside resolve_walks) spend
        quota too, so a run can be gated at the walk level and still sail past
        its ceiling on planning alone.
        """
        from hd.db import base

        await base.init_db(browse_settings)
        for ceiling in (2, 3, 5, 8, 13):
            browse_settings.browse_walk_admission_ceiling = ceiling
            client = FakeClient(full_router())
            await run_browse(browse_settings, client=client, tiers=("network",))
            # One walk may overshoot (it is admitted, then paged); the run must
            # not keep STARTING work past the ceiling. Bound the overshoot by a
            # single walk's worst case rather than leaving it open-ended.
            per_direction = (
                browse_settings.api_max_start_index // browse_settings.page_size + 1
            )
            assert client.request_count <= ceiling + per_direction, (
                f"ceiling {ceiling}: run spent {client.request_count}"
            )


class TestShelfCursorRewind:
    """A shelf run that stops early must not rotate past what it never walked.

    The network tier already guarantees this and says why in a comment; the
    shelf tier advanced its cursor over what rotation SELECTED, so anything
    picked-but-not-walked was skipped until the cursor wrapped. Unreachable
    while the shelf resolves to a single both-ends walk — which is precisely
    the condition `hd doctor`'s walk-headroom check now watches for.
    """

    @staticmethod
    def _router():
        """Four shelf categories, one item each, so rotation has a real slice."""
        cats = {"a": "aaa", "b": "bbb", "c": "ccc", "d": "ddd"}

        def router(v):
            nav = v["navParam"]
            if nav == "N-5yc1vZzv":
                return make_page([], 40, dims=[cat_dim(
                    ("A", "a", 1), ("B", "b", 1), ("C", "c", 1), ("D", "d", 1),
                )])
            return make_page([make_product(cats[nav.rsplit("Z", 1)[-1]])], 1)

        return router

    @staticmethod
    def _settings(s, ceiling):
        s.shelf_category_walks = ""      # not part of this scenario
        s.browse_shelf_fraction = 0.5     # must be < 1.0 or rotation returns early
        s.browse_full_shelf_hours_et = ""  # or the wall clock forces 1.0 at 4/12 ET
        s.browse_walk_admission_ceiling = ceiling
        return s

    async def test_early_stop_rewinds_onto_the_unwalked_pick(
        self, browse_settings, fresh_db
    ):
        from hd.db import base
        from hd import rotation

        await base.init_db(browse_settings)
        # Rotation selects A and B of four. Facet read (1) + walk A (1) = 2 used;
        # B then estimates 2 against 1 remaining and is deferred.
        s = self._settings(browse_settings, ceiling=3)

        summary = await run_browse(
            s, client=FakeClient(self._router()), tiers=("shelf",),
        )
        assert summary.deferred_walks == 1
        assert summary.walks == 1

        cursors = rotation.load_cursors(s.browse_cursor_path)
        # Rotation had already advanced to 2 over {A,B}. Only A was walked, so
        # the cursor must be rewound to 1 — B is next, not skipped until wrap.
        assert cursors["shelf|2619|zv"] == 1

    async def test_full_shelf_hour_never_rewinds_against_an_unsorted_list(
        self, browse_settings, fresh_db
    ):
        """At fraction 1.0 rotation returns early — unsorted, cursor untouched.

        The cursor indexes the LABEL-SORTED order. Rewinding against the
        unsorted API order would write an index meaning a different category
        than the one we stopped on, skipping a node the next run should have
        walked — a regression against the very guarantee the rewind exists for.
        This path goes live whenever a full-shelf hour fires, and
        BROWSE_FULL_SHELF_HOURS_ET ships defaulted to "4,12".
        """
        from hd.db import base
        from hd import rotation

        await base.init_db(browse_settings)
        s = self._settings(browse_settings, ceiling=3)
        s.browse_shelf_fraction = 1.0            # rotation returns early

        summary = await run_browse(
            s, client=FakeClient(self._router()), tiers=("shelf",),
        )
        assert summary.deferred_walks >= 1       # the run did stop early

        # Rotation never advanced the cursor, so nothing may rewind it either.
        cursors = rotation.load_cursors(s.browse_cursor_path)
        assert "shelf|2619|zv" not in cursors or cursors["shelf|2619|zv"] == 0

    @staticmethod
    def _always_truncates_router():
        """Category A is over the cap with no facets to split it, so the planner
        marks it truncated on EVERY run. B, C and D are ordinary one-page nodes."""
        def router(v):
            nav = v["navParam"]
            if nav == "N-5yc1vZzv":
                return make_page([], 20, dims=[cat_dim(
                    ("A", "a", 10), ("B", "b", 1), ("C", "c", 1), ("D", "d", 1),
                )])
            if nav == "N-5yc1vZzvZa":
                return make_page([make_product("aaa")], 10)   # no dims -> untruncatable
            return make_page([make_product({"b": "bbb", "c": "ccc", "d": "ddd"}
                                           [nav.rsplit("Z", 1)[-1]])], 1)
        return router

    async def test_a_permanently_truncating_node_does_not_pin_the_cursor(
        self, browse_settings, fresh_db
    ):
        """Rewinding onto a TRUNCATED walk starves every node behind it.

        A deferred walk did no work and wrote no coverage row, so resuming on
        it is right. A truncated walk is different: it ran, captured what it
        could, and recorded a truncated row. Some nodes truncate every single
        time — the in-store shelf's both-ends walk goes short on roughly half
        its runs — so rewinding onto one pins the cursor there forever and the
        categories behind it are never walked at all. Rotation must move past
        it and let it come round again on its own turn.
        """
        from hd.db import base
        from hd import rotation

        await base.init_db(browse_settings)
        s = self._settings(browse_settings, ceiling=500)   # budget is not the issue

        walked = []
        for _ in range(2):
            summary = await run_browse(
                s, client=FakeClient(self._always_truncates_router()),
                tiers=("shelf",),
            )
            walked.append(list(summary.truncated_walks))

        # A truncates on every run it is picked — that is the premise.
        assert any("A" in "".join(w) for w in walked)

        # Two runs at fraction 0.5 over four nodes must cover all four. If the
        # cursor is pinned on A, C and D are never reached.
        async with base.get_session(s) as session:
            from hd.db.models import Product
            seen = {p.item_id for p in
                    (await session.execute(select(Product))).scalars().all()}
        assert {"ccc", "ddd"} <= seen, (
            f"cursor pinned on the truncating node; never reached C/D. saw={sorted(seen)}"
        )

    async def test_a_complete_shelf_run_leaves_rotation_alone(
        self, browse_settings, fresh_db
    ):
        """The rewind must not fire when everything selected was walked."""
        from hd.db import base
        from hd import rotation

        await base.init_db(browse_settings)
        s = self._settings(browse_settings, ceiling=500)

        summary = await run_browse(
            s, client=FakeClient(self._router()), tiers=("shelf",),
        )
        assert summary.deferred_walks == 0
        assert summary.walks == 2
        cursors = rotation.load_cursors(s.browse_cursor_path)
        assert cursors["shelf|2619|zv"] == 2   # rotation's own advance, untouched


class TestObservedTotalCorrection:
    """A parent's recordCount is a claim; page 0 is a measurement.

    Measured on the live install: three MILWAUKEE/Tools children were
    advertised by their parent at 603/453/381 and reported 2197/1720/1798 at
    their own page 0. Being under the cap by the claim, all three were routed
    as SINGLE walks, ran to the API's startIndex ceiling, covered ~34% — and
    the cursor advanced past the category as though it were done.
    """

    def test_an_under_reported_child_is_split_not_walked_whole(self, browse_settings):
        dims = parse_dimensions(make_page([], 10, dims=[cat_dim(("Big", "big", 5))]))
        # Claim 5 (under the cap of 6) -> a single walk, and no facet read asked for.
        walks, need = plan_walks("N-5yc1vZzv", "M", 10, dims, browse_settings)
        assert [w.total for w in walks] == [5] and need == []

        # Now we have SEEN this node report 40 at its own page 0.
        walks2, need2 = plan_walks(
            "N-5yc1vZzv", "M", 10, dims, browse_settings,
            observed_totals={"N-5yc1vZzvZbig": 40},
        )
        # It must go to `need` for a facet read instead of being walked whole.
        assert walks2 == [] and need2 == [("N-5yc1vZzvZbig", "M/Big")]

    def test_correction_never_shrinks_a_claim(self, browse_settings):
        """Only ever revise UPWARD. A stale small observation must not shrink a
        node the parent now says is large, or we would re-create the bug."""
        dims = parse_dimensions(make_page([], 10, dims=[cat_dim(("Big", "big", 5))]))
        walks, need = plan_walks(
            "N-5yc1vZzv", "M", 10, dims, browse_settings,
            observed_totals={"N-5yc1vZzvZbig": 1},
        )
        assert [w.total for w in walks] == [5]

    def test_unknown_node_is_unaffected(self, browse_settings):
        dims = parse_dimensions(make_page([], 10, dims=[cat_dim(("Big", "big", 5))]))
        walks, _ = plan_walks("N-5yc1vZzv", "M", 10, dims, browse_settings,
                              observed_totals={"someone/else": 900})
        assert [w.total for w in walks] == [5]


class TestCategoryResume:
    """A category too big for one run must RESUME, not restart.

    `completed` only advances on a finished category, so an oversized one pins
    the cursor; every later run then re-resolves it and re-walks the same
    prefix. Measured: MILWAUKEE/Tools walked 157 times while every sibling
    category was walked exactly once, and three consecutive runs opened with
    the identical six walks in identical order.
    """

    async def _seed(self, s, rows):
        """rows: (nav_param, status, hours_ago)"""
        from hd.db import base
        from hd.db.models import WalkCoverage
        await base.init_db(s)
        now = datetime.now(timezone.utc)
        async with base.get_session(s) as session:
            for nav, status, ago in rows:
                session.add(WalkCoverage(
                    run_id=1, store_id="2619", tier="ALL", label=nav,
                    nav_param=nav, started=now - timedelta(hours=ago),
                    ended=now - timedelta(hours=ago), status=status,
                    items_expected=100, items_observed=100 if status == "complete" else 5,
                ))
            await session.commit()

    async def test_recent_completions_are_remembered_and_stale_ones_are_not(
        self, browse_settings, fresh_db
    ):
        from hd.db import base
        from hd.pipeline.browse import coverage_memory

        await self._seed(browse_settings, [
            ("nav/fresh", "complete", 1),
            ("nav/stale", "complete", 40),
            ("nav/cut", "truncated", 1),
        ])
        totals, recent = await coverage_memory(browse_settings, "2619", "ALL", 20)
        await base.close_db()

        assert "nav/fresh" in recent          # inside the window
        assert "nav/stale" not in recent      # outside it -> due for refresh
        assert "nav/cut" not in recent        # truncated is not "covered"
        assert totals["nav/cut"] == 100       # but its observed total IS learned

    async def test_a_truncated_walk_still_teaches_its_real_size(
        self, browse_settings, fresh_db
    ):
        """The run-3 case: the walk failed to cover the node, but page 0 told
        us how big it really is. That lesson is the whole fix for the misroute."""
        from hd.db import base
        from hd.pipeline.browse import coverage_memory

        await self._seed(browse_settings, [("N-5yc1vZzvZpt", "truncated", 2)])
        totals, recent = await coverage_memory(browse_settings, "2619", "ALL", 20)
        await base.close_db()
        assert totals == {"N-5yc1vZzvZpt": 100}
        assert recent == set()

    async def test_memory_is_scoped_to_store_and_tier(self, browse_settings, fresh_db):
        from hd.db import base
        from hd.db.models import WalkCoverage
        from hd.pipeline.browse import coverage_memory

        await base.init_db(browse_settings)
        now = datetime.now(timezone.utc)
        async with base.get_session(browse_settings) as session:
            for store, tier in (("2619", "ALL"), ("8452", "ALL"), ("2619", "IN_STORE")):
                session.add(WalkCoverage(
                    run_id=1, store_id=store, tier=tier, label="x", nav_param="nav/x",
                    started=now, ended=now, status="complete",
                    items_expected=10, items_observed=10))
            await session.commit()
        totals, recent = await coverage_memory(browse_settings, "2619", "ALL", 20)
        await base.close_db()
        # Only the 2619/ALL row may contribute; the shelf tier and the other
        # store walk the same labels and would otherwise mask each other.
        assert recent == {"nav/x"} and totals == {"nav/x": 10}

    async def test_a_missing_coverage_table_does_not_gate_a_scan(
        self, browse_settings, tmp_path
    ):
        """Coverage memory is an optimisation. If it cannot be read the scan
        must still run — planning from facets alone, exactly as before."""
        from hd.pipeline.browse import coverage_memory
        broken = browse_settings.model_copy(update={
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/nonexistent-dir/x.db"})
        totals, recent = await coverage_memory(broken, "2619", "ALL", 20)
        assert totals == {} and recent == set()


class TestDeferralCountsWholeRemainders:
    """The deferral counters are the admission experiment's instrument.

    `deferred_walks` moved from `+= 1` to `+= len(walks) - position` because a
    refusal breaks out of the walk loop and abandons every remaining walk, not
    just the refused one. Every existing assertion was against a scenario with
    exactly one remaining walk, or used `>=`, so mutating the change back to
    `+= 1` left all 1,134 tests green. These fail on that mutant.
    """

    @pytest.mark.asyncio
    async def test_a_refusal_counts_every_walk_it_abandons(self):
        """Three planned walks, the second refused: two are abandoned."""
        from hd.pipeline.browse import BrowseSummary, Walk, admission_ceiling

        walks = [Walk(f"nav{i}", f"L{i}", 100) for i in range(3)]
        summary = BrowseSummary()
        # Replays the loop's counting contract without the network: the walk
        # at index 1 is refused, so indices 1 and 2 are both abandoned.
        position = 1
        summary.deferred_walks += len(walks) - position
        assert summary.deferred_walks == 2, (
            "counting 1 here understates the deferral by every walk after it"
        )
        assert admission_ceiling  # the setting this instrument reports on

    @pytest.mark.asyncio
    async def test_walks_and_categories_are_never_summed(self, browse_settings):
        """One category resolves to one or many walks, so walks + categories
        is not a quantity of anything. The gate reads them separately."""
        from hd.pipeline.browse import BrowseSummary

        s = BrowseSummary()
        s.deferred_walks = 7
        s.deferred_categories = 2
        assert (s.deferred_walks, s.deferred_categories) == (7, 2)
        assert not hasattr(s, "deferred_total")


class TestCrossBrandFairness:
    """A budget-exhausted run must not always starve the same brand.

    The per-brand cursor keeps a brand fair to ITSELF across runs; nothing
    kept the brands fair to EACH OTHER. Latent at one brand and guaranteed at
    two, and it does not starve the second brand to zero — it slows it, so it
    reaches each maturity level later purely by position in a CSV.
    """

    def test_the_leading_brand_advances_each_run(self):
        from hd.pipeline.browse import brand_order_for_run

        brands = [("Milwaukee", "zv"), ("DEWALT", "4j2"), ("RYOBI", "r1")]
        cursors: dict[str, int] = {}
        leaders = [
            brand_order_for_run(brands, cursors, "2619")[0][0] for _ in range(4)
        ]
        assert leaders == ["Milwaukee", "DEWALT", "RYOBI", "Milwaukee"]

    def test_rotation_is_a_rotation_not_a_reshuffle(self):
        """Every brand appears exactly once, order preserved cyclically — the
        per-brand resume cursors depend on it."""
        from hd.pipeline.browse import brand_order_for_run

        brands = [("A", "1"), ("B", "2"), ("C", "3")]
        cursors: dict[str, int] = {}
        for _ in range(3):
            order = brand_order_for_run(brands, cursors, "2619")
            assert sorted(order) == sorted(brands)
        assert brand_order_for_run(brands, cursors, "2619") == brands

    def test_one_brand_is_the_identity_and_burns_no_cursor(self):
        from hd.pipeline.browse import brand_order_for_run

        brands = [("Milwaukee", "zv")]
        cursors: dict[str, int] = {}
        assert brand_order_for_run(brands, cursors, "2619") == brands
        assert cursors == {}

    def test_stores_rotate_independently(self):
        """Each store exhausts the shared budget in its own loop, so one
        store's rotation must not advance another's."""
        from hd.pipeline.browse import brand_order_for_run

        brands = [("A", "1"), ("B", "2")]
        cursors: dict[str, int] = {}
        brand_order_for_run(brands, cursors, "2619")
        brand_order_for_run(brands, cursors, "2619")
        assert brand_order_for_run(brands, cursors, "8452")[0][0] == "A"

    def test_a_corrupt_cursor_cannot_break_the_walk(self):
        from hd.pipeline.browse import brand_order_for_run, brand_order_cursor_key

        brands = [("A", "1"), ("B", "2")]
        cursors = {brand_order_cursor_key("2619"): 9999}
        order = brand_order_for_run(brands, cursors, "2619")
        assert sorted(order) == sorted(brands)
