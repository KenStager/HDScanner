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
        daily_deals_evidence_path=str(tmp_path / "evidence.jsonl"),
        # The suite below exercises the brand filter itself, so probing is
        # opt-in per test. TestProbesUnknownItems covers the shipped default.
        daily_deals_probe_unknown=0,
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

    async def test_set_with_no_tracked_brands_costs_nothing_when_probing_is_off(self, dd_settings, fresh_db):
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



def _evidence(settings) -> list[dict]:
    from pathlib import Path

    path = Path(settings.daily_deals_evidence_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@pytest.fixture
def poll_settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/dd.db",
        stores="2619",
        brands="Milwaukee",
        daily_deals_cursor_path=str(tmp_path / "dd_cursor"),
        daily_deals_evidence_path=str(tmp_path / "diag" / "polls.jsonl"),
        daily_deals_poll_seconds=120,
        daily_deals_poll_max=6,
        daily_deals_poll_jitter_seconds=0,
        daily_deals_poll_phase_seconds=0,
        daily_deals_probe_unknown=0,
        throttle_cooldown_path=str(tmp_path / "cooldown"),
        store_raw_json=False,
    )


class FakeSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _sets(*end_dates):
    """A fetch stub that returns one parsed set per call, in order."""
    queue = [
        None if e is None else DailyDealSet(end_date=e, item_ids=["111", "222"])
        for e in end_dates
    ]

    async def fetch(_settings):
        return queue.pop(0)

    return fetch


def _at(hour: int, minute: int, second: int = 0):
    """A clock frozen at an Eastern wall time on the test date."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fixed = datetime(2026, 9, 3, hour, minute, second, tzinfo=ZoneInfo("America/New_York"))
    return lambda: fixed


def _at_on(day: int, hour: int, minute: int, second: int = 0):
    """A clock frozen at an Eastern wall time on a chosen September 2026 date."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fixed = datetime(2026, 9, day, hour, minute, second, tzinfo=ZoneInfo("America/New_York"))
    return lambda: fixed


ON_REFRESH = _at(3, 0, 20)
# 2026-09-03 has an even ordinal and 2026-09-04 an odd one, so these two are
# the even-phase and odd-phase nights the alternation distinguishes.
ON_REFRESH_ODD_NIGHT = _at_on(4, 3, 0, 20)


class TestWaitForRefresh:
    async def test_flips_on_the_third_read_and_sweeps_that_set_once(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        await seed_catalog(poll_settings, **{"111": "Milwaukee", "222": "Milwaukee"})
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-03")
        sleep = FakeSleep()
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-03", "2026-09-03", "2026-09-04")):
            summary = await wait_for_refresh(poll_settings, client=client, sleep=sleep, now_et=ON_REFRESH)

        assert summary.skipped is False
        assert summary.polls == 3
        assert summary.end_date == "2026-09-04"
        assert summary.seconds_to_flip is not None
        # Two unchanged reads, two waits, then the flip is swept from the set
        # already in hand — no fourth fetch, and each item priced exactly once.
        assert sleep.calls == [120, 120]
        assert client.requested == ["111", "222"]
        assert summary.snapshots == 2
        assert Path(poll_settings.daily_deals_cursor_path).read_text() == "2026-09-04"
        phases = [e["phase"] for e in _evidence(poll_settings)]
        # The partition is recorded once, between the flip and the first price:
        # what the set was measured against, before anything was requested.
        assert phases == ["poll", "poll", "flip", "partition", "priced", "priced", "swept"]
        flip = next(e for e in _evidence(poll_settings) if e["phase"] == "flip")
        assert flip["previous"] == "2026-09-03" and flip["end_date"] == "2026-09-04"
        assert flip["poll"] == 3 and "seconds_after_start" in flip

    async def test_first_run_takes_the_first_read_as_baseline_not_as_a_flip(self, poll_settings, fresh_db):
        """With no cursor the page may still show the expiring set. Sweeping it
        would write its cursor and hand the real set to the routine run."""
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        await seed_catalog(poll_settings, **{"111": "Milwaukee"})
        sleep = FakeSleep()
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-03", "2026-09-04")):
            summary = await wait_for_refresh(poll_settings, client=client, sleep=sleep, now_et=ON_REFRESH)

        assert summary.skipped is False and summary.polls == 2
        assert summary.end_date == "2026-09-04"
        assert sleep.calls == [120]
        assert client.requested == ["111"]
        assert Path(poll_settings.daily_deals_cursor_path).read_text() == "2026-09-04"
        assert [e["phase"] for e in _evidence(poll_settings)][:2] == ["baseline", "flip"]

    async def test_first_run_with_no_flip_leaves_the_set_to_the_routine_sweep(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        poll_settings.daily_deals_poll_max = 2
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-03", "2026-09-03")):
            summary = await wait_for_refresh(poll_settings, client=client, sleep=FakeSleep(), now_et=ON_REFRESH)

        assert summary.skipped is True and summary.stopped == "unchanged"
        assert client.requested == []
        # No cursor was written: the routine run must still sweep this set.
        assert not Path(poll_settings.daily_deals_cursor_path).exists()

    async def test_unavailable_page_stops_without_retry(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-03")
        sleep = FakeSleep()
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-03", None, "2026-09-04")):
            summary = await wait_for_refresh(poll_settings, client=client, sleep=sleep, now_et=ON_REFRESH)

        # One good read, one wait, then a refusal: stop. The third set is
        # never asked for and nothing is priced.
        assert summary.skipped is True and summary.stopped == "unavailable"
        assert summary.polls == 2
        assert sleep.calls == [120]
        assert client.requested == []
        assert [e["phase"] for e in _evidence(poll_settings)] == ["poll", "unavailable"]
        assert Path(poll_settings.daily_deals_cursor_path).read_text() == "2026-09-03"

    async def test_cap_reached_leaves_the_set_to_the_routine_sweep(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-03")
        poll_settings.daily_deals_poll_max = 3
        sleep = FakeSleep()
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-03", "2026-09-03", "2026-09-03", "2026-09-04")):
            summary = await wait_for_refresh(poll_settings, client=client, sleep=sleep, now_et=ON_REFRESH)

        assert summary.skipped is True and summary.stopped == "unchanged"
        assert summary.polls == 3
        # No wait after the last read: there is nothing to wait for.
        assert sleep.calls == [120, 120]
        assert client.requested == []
        assert [e["phase"] for e in _evidence(poll_settings)] == ["poll", "poll", "poll"]

    async def test_older_end_date_is_not_a_flip(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-03")
        poll_settings.daily_deals_poll_max = 2
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-02", "2026-09-02")):
            summary = await wait_for_refresh(poll_settings, client=client, sleep=FakeSleep(), now_et=ON_REFRESH)

        assert summary.skipped is True and summary.stopped == "unchanged"
        assert client.requested == []
        assert Path(poll_settings.daily_deals_cursor_path).read_text() == "2026-09-03"
        assert [e["phase"] for e in _evidence(poll_settings)] == ["older", "older"]

    async def test_late_start_takes_one_read_and_stops(self, poll_settings, fresh_db):
        """A slot launchd ran hours late must not spend six reads mid-day."""
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-04")
        sleep = FakeSleep()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-04", "2026-09-05")):
            summary = await wait_for_refresh(poll_settings, client=FakeClient(), sleep=sleep, now_et=_at(11, 30))

        assert summary.skipped is True and summary.stopped == "late"
        assert summary.polls == 1
        assert sleep.calls == []

    async def test_late_start_still_sweeps_a_pending_flip(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        await seed_catalog(poll_settings, **{"111": "Milwaukee"})
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-03")
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", side_effect=_sets("2026-09-04")):
            summary = await wait_for_refresh(poll_settings, client=client, sleep=FakeSleep(), now_et=_at(11, 30))

        assert summary.skipped is False and summary.polls == 1
        assert client.requested == ["111"]

    async def test_cooldown_defers_before_any_read(self, poll_settings):
        from hd.http.cooldown import ThrottleCooldown
        from hd.pipeline.daily_deals import wait_for_refresh

        ThrottleCooldown(poll_settings.throttle_cooldown_path, 3600).start()
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set") as fetch:
            summary = await wait_for_refresh(poll_settings, sleep=FakeSleep(), now_et=ON_REFRESH)

        assert summary.skipped is True and summary.stopped == "cooldown"
        fetch.assert_not_called()
        assert [e["phase"] for e in _evidence(poll_settings)] == ["cooldown"]

    async def test_disabled_does_nothing(self, poll_settings):
        from hd.pipeline.daily_deals import wait_for_refresh

        poll_settings.daily_deals_enabled = False
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set") as fetch:
            summary = await wait_for_refresh(poll_settings, sleep=FakeSleep(), now_et=ON_REFRESH)
        assert summary.skipped is True and summary.stopped == "disabled"
        fetch.assert_not_called()

    async def test_jitter_only_ever_delays(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(poll_settings)
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-03")
        poll_settings.daily_deals_poll_max = 3
        poll_settings.daily_deals_poll_jitter_seconds = 15
        sleep = FakeSleep()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   side_effect=_sets("2026-09-03", "2026-09-03", "2026-09-03")):
            await wait_for_refresh(poll_settings, client=FakeClient(), sleep=sleep, now_et=ON_REFRESH)

        # A start offset (possibly zero) then two stretched intervals.
        assert len(sleep.calls) in (2, 3)
        intervals = sleep.calls[-2:]
        assert all(120 <= s <= 135 for s in intervals)
        if len(sleep.calls) == 3:
            assert 0 < sleep.calls[0] <= 15


class TestSweepLock:
    async def test_second_sweep_of_the_same_set_is_refused(self, poll_settings, fresh_db):
        from hd.db import base
        from hd.pipeline.daily_deals import _SweepLock

        await base.init_db(poll_settings)
        await seed_catalog(poll_settings, **{"111": "Milwaukee"})
        deal_set = DailyDealSet(end_date="2026-09-04", item_ids=["111"])
        holder = _SweepLock(poll_settings)
        assert holder.acquire() is True
        try:
            client = FakeClient()
            summary = await run_daily_deals(poll_settings, client=client, deal_set=deal_set)
        finally:
            holder.release()

        assert summary.skipped is True and summary.stopped == "locked"
        assert client.requested == []
        assert [e["phase"] for e in _evidence(poll_settings)] == ["locked"]

        # Released: the same call now sweeps.
        summary = await run_daily_deals(poll_settings, client=FakeClient(), deal_set=deal_set)
        assert summary.skipped is False and summary.snapshots == 1


class TestEvidenceFile:
    def test_digest_is_order_sensitive_and_stable(self):
        from hd.pipeline.daily_deals import list_digest

        assert list_digest(["1", "2"]) == list_digest(["1", "2"])
        assert list_digest(["1", "2"]) != list_digest(["2", "1"])
        assert len(list_digest([])) == 16

    def test_line_shape_and_missing_directory(self, poll_settings):
        from hd.pipeline.daily_deals import record_evidence

        deal_set = DailyDealSet(
            end_date="2026-09-04", item_ids=["1", "2"],
            categories=[{"name": "Deals of the Day", "item_ids": ["1", "2"]}],
        )
        record_evidence(poll_settings, "poll", deal_set, poll=1, cursor="2026-09-03")
        (line,) = _evidence(poll_settings)
        assert line["phase"] == "poll" and line["end_date"] == "2026-09-04"
        assert line["items"] == 2 and len(line["list_sha256"]) == 16
        assert line["categories"] == ["Deals of the Day"]
        assert line["poll"] == 1 and line["cursor"] == "2026-09-03"
        assert line["ts"].endswith("+00:00")

    def test_relative_path_is_refused_under_pytest(self, poll_settings, tmp_path, monkeypatch):
        """The suite must never forge lines into a real evidence file."""
        from pathlib import Path
        from hd.pipeline.daily_deals import record_evidence

        monkeypatch.chdir(tmp_path)
        poll_settings.daily_deals_evidence_path = "diagnostics/daily_deals_polls.jsonl"
        record_evidence(poll_settings, "poll")
        assert not Path(tmp_path / "diagnostics" / "daily_deals_polls.jsonl").exists()

    def test_unwritable_path_is_a_warning_not_a_failure(self, poll_settings, tmp_path):
        from hd.pipeline.daily_deals import record_evidence

        blocker = tmp_path / "blocker"
        blocker.write_text("")
        poll_settings.daily_deals_evidence_path = str(blocker / "polls.jsonl")
        record_evidence(poll_settings, "poll")  # must not raise

    def test_rolls_one_generation_past_the_cap(self, poll_settings, monkeypatch):
        from pathlib import Path
        from hd.pipeline import daily_deals
        from hd.pipeline.daily_deals import record_evidence

        monkeypatch.setattr(daily_deals, "_EVIDENCE_MAX_BYTES", 10)
        record_evidence(poll_settings, "poll")
        record_evidence(poll_settings, "poll")
        path = Path(poll_settings.daily_deals_evidence_path)
        rolled = path.with_suffix(".1.jsonl")
        assert rolled.exists() and len(rolled.read_text().splitlines()) == 1
        assert len(path.read_text().splitlines()) == 1

    async def test_already_processed_read_is_recorded(self, poll_settings, fresh_db):
        from pathlib import Path
        from hd.db import base

        await base.init_db(poll_settings)
        Path(poll_settings.daily_deals_cursor_path).write_text("2026-09-03")
        deal_set = DailyDealSet(end_date="2026-09-03", item_ids=["111"])

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary = await run_daily_deals(poll_settings, client=FakeClient())

        assert summary.skipped is True
        (line,) = _evidence(poll_settings)
        assert line["phase"] == "already_processed" and line["cursor"] == "2026-09-03"
        assert line["items"] == 1

    async def test_priced_line_carries_the_snapshot_fields(self, poll_settings, fresh_db):
        from hd.db import base

        await base.init_db(poll_settings)
        await seed_catalog(poll_settings, **{"111": "Milwaukee"})
        deal_set = DailyDealSet(end_date="2026-09-04", item_ids=["111"])

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            await run_daily_deals(poll_settings, client=FakeClient())

        lines = _evidence(poll_settings)
        priced = [e for e in lines if e["phase"] == "priced"]
        assert len(priced) == 1 and priced[0]["item_id"] == "111"
        (price,) = priced[0]["prices"]
        assert price["store_id"] == "2619" and price["price_value"] is not None
        assert {"ts", "promotion_tag", "savings_center", "out_of_stock"} <= set(price)
        swept = lines[-1]
        assert swept["phase"] == "swept" and swept["cursor_saved"] is True
        assert swept["api_requests"] == 1 and swept["tracked"] == 1

    async def test_failed_database_write_keeps_the_observation_and_not_the_cursor(self, poll_settings, fresh_db):
        """The priced line is written before the database write, so a locked
        database loses the write and not the observation — and must not save
        the cursor, or the routine sweep would skip the set."""
        from pathlib import Path
        from hd.db import base

        await base.init_db(poll_settings)
        await seed_catalog(poll_settings, **{"111": "Milwaukee"})
        deal_set = DailyDealSet(end_date="2026-09-04", item_ids=["111"])

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set), \
             patch("hd.pipeline.snapshot._insert_snapshots", side_effect=RuntimeError("database is locked")), \
             pytest.raises(RuntimeError):
            await run_daily_deals(poll_settings, client=FakeClient())

        phases = [e["phase"] for e in _evidence(poll_settings)]
        assert "priced" in phases
        assert phases[-1] == "swept"
        assert _evidence(poll_settings)[-1]["cursor_saved"] is False
        assert not Path(poll_settings.daily_deals_cursor_path).exists()


class TestPollPhaseAlternates:
    """The six reads are held back half an interval on alternate nights, so
    across nights they fall on minutes a fixed series never samples."""

    async def _run(self, settings, clock, sets, cursor):
        from pathlib import Path
        from hd.db import base
        from hd.pipeline.daily_deals import wait_for_refresh

        await base.init_db(settings)
        Path(settings.daily_deals_cursor_path).write_text(cursor)
        sleep = FakeSleep()
        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", side_effect=_sets(*sets)):
            summary = await wait_for_refresh(
                settings, client=FakeClient(), sleep=sleep, now_et=clock,
            )
        return summary, sleep

    async def test_even_night_starts_on_the_refresh(self, poll_settings, fresh_db):
        poll_settings.daily_deals_poll_phase_seconds = 60
        poll_settings.daily_deals_poll_max = 2
        _, sleep = await self._run(
            poll_settings, ON_REFRESH, ("2026-09-03", "2026-09-03"), "2026-09-03",
        )
        # Only the interval between the two reads — nothing before the first.
        assert sleep.calls == [120]

    async def test_odd_night_holds_the_first_read_back_one_half_interval(
        self, poll_settings, fresh_db,
    ):
        poll_settings.daily_deals_poll_phase_seconds = 60
        poll_settings.daily_deals_poll_max = 2
        _, sleep = await self._run(
            poll_settings, ON_REFRESH_ODD_NIGHT, ("2026-09-04", "2026-09-04"), "2026-09-04",
        )
        assert sleep.calls == [60, 120]

    async def test_zero_disables_the_alternation(self, poll_settings, fresh_db):
        poll_settings.daily_deals_poll_phase_seconds = 0
        poll_settings.daily_deals_poll_max = 2
        _, sleep = await self._run(
            poll_settings, ON_REFRESH_ODD_NIGHT, ("2026-09-04", "2026-09-04"), "2026-09-04",
        )
        assert sleep.calls == [120]

    async def test_a_late_start_is_not_delayed_further(self, poll_settings, fresh_db):
        """Outside the window the poll takes its one read now: the slot is
        already missed, and waiting would only age the reading."""
        poll_settings.daily_deals_poll_phase_seconds = 60
        summary, sleep = await self._run(
            poll_settings, _at_on(4, 22, 31), ("2026-09-04",), "2026-09-04",
        )
        assert sleep.calls == []
        assert summary.polls == 1 and summary.stopped == "late"

    async def test_flip_evidence_records_the_night_s_phase(self, poll_settings, fresh_db):
        poll_settings.daily_deals_poll_phase_seconds = 60
        summary, _ = await self._run(
            poll_settings, ON_REFRESH_ODD_NIGHT, ("2026-09-05",), "2026-09-04",
        )
        flip = [e for e in _evidence(poll_settings) if e["phase"] == "flip"]
        assert len(flip) == 1
        assert flip[0]["start_phase_seconds"] == 60
        assert flip[0]["end_date"] == "2026-09-05"


class TestProbesUnknownItems:
    """The catalog can only recognise a tool already in it, so the one case the
    brand filter is blind to is a tracked brand reaching the deals for the first
    time. Owner decision 2026-09-03: spend a request per unknown id to see it."""

    async def test_probing_is_off_by_default(self):
        # Reverted 2026-09-03: a probe is discarded unless it is our brand, so
        # the spend never decays, and recording it instead would enrol non-tool
        # items in the snapshot rotation. The partition line sizes the blind
        # spot for nothing; raise this only against those counts.
        assert Settings().daily_deals_probe_unknown == 0

    async def test_unknown_ids_are_requested_by_default(self, dd_settings, fresh_db):
        from hd.db import base
        from hd.pipeline.daily_deals import run_daily_deals

        await base.init_db(dd_settings)
        await seed_catalog(dd_settings, **{"111": "Milwaukee"})
        dd_settings.daily_deals_probe_unknown = 250
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=["111", "222", "333"])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary = await run_daily_deals(dd_settings, client=client)

        # The catalogued item first, then every id the catalog could not answer for.
        assert client.requested == ["111", "222", "333"]
        assert summary.skipped_unknown == 0

    async def test_probing_does_not_outrun_the_request_budget(self, dd_settings, fresh_db):
        """The sweep sizes its own client budget to the set, so probing every
        unknown cannot starve the items it was going to price."""
        from hd.db import base
        from hd.pipeline.daily_deals import run_daily_deals

        await base.init_db(dd_settings)
        await seed_catalog(dd_settings, **{"100": "Milwaukee"})
        dd_settings.daily_deals_probe_unknown = 250
        ids = [str(i) for i in range(100, 210)]  # 110 ids, one of them tracked
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set",
                   return_value=DailyDealSet(end_date="2026-08-18", item_ids=ids)):
            summary = await run_daily_deals(dd_settings, client=client)

        assert len(client.requested) == 110
        assert summary.skipped_unknown == 0
        assert summary.aborted is False


class TestCatalogPartition:
    """Sizing the blind spot must cost no requests: it is a database question."""

    async def test_partition_counts_the_three_kinds_and_spends_nothing(
        self, dd_settings, fresh_db,
    ):
        from hd.db import base
        from hd.pipeline.daily_deals import run_daily_deals

        await base.init_db(dd_settings)
        # 111 ours, 333 already answered "not ours", 777/888 never seen.
        await seed_catalog(dd_settings, **{"111": "Milwaukee", "333": "RYOBI"})
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=["111", "333", "777", "888"])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            await run_daily_deals(dd_settings, client=client)

        part = [e for e in _evidence(dd_settings) if e["phase"] == "partition"]
        assert len(part) == 1
        assert part[0]["tracked"] == 1
        assert part[0]["known_not_ours"] == 1
        assert part[0]["never_seen"] == 2
        # Only the tracked item was requested: measuring cost no requests.
        assert client.requested == ["111"]

    async def test_an_already_answered_id_is_never_re_probed(self, dd_settings, fresh_db):
        """A probe budget targets the blind spot, not questions we have paid for."""
        from hd.db import base
        from hd.pipeline.daily_deals import run_daily_deals

        await base.init_db(dd_settings)
        await seed_catalog(dd_settings, **{"333": "RYOBI"})
        dd_settings.daily_deals_probe_unknown = 250
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=["333", "777"])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            await run_daily_deals(dd_settings, client=client)

        # 333 is known not to be ours; only the never-seen 777 is worth asking about.
        assert client.requested == ["777"]

    async def test_partition_is_recorded_even_when_nothing_is_ours(
        self, dd_settings, fresh_db,
    ):
        """The night that spends nothing is exactly the night whose blind-spot
        size we need, so the measurement cannot sit behind the early return."""
        from hd.db import base
        from hd.pipeline.daily_deals import run_daily_deals

        await base.init_db(dd_settings)
        deal_set = DailyDealSet(end_date="2026-08-18", item_ids=[str(i) for i in range(100, 184)])
        client = FakeClient()

        with patch("hd.pipeline.daily_deals.fetch_daily_deal_set", return_value=deal_set):
            summary = await run_daily_deals(dd_settings, client=client)

        part = [e for e in _evidence(dd_settings) if e["phase"] == "partition"]
        assert len(part) == 1 and part[0]["never_seen"] == 84
        assert client.requested == [] and summary.items_checked == 0
