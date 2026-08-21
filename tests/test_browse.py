"""Tests for the facet-driven brand browse pipeline."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from hd.config import Settings
from hd.hd_api.parsers import parse_dimensions
from hd.pipeline.browse import (
    Walk,
    effective_shelf_fraction,
    full_shelf_hours,
    rotate_shelf_walks,
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
