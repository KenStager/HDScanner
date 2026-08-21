"""The health banner: the push half of diagnostics that were only ever pull.

The checks themselves are covered in test_doctor.py. What matters here is that
putting them on an always-open page cannot make the page slow, and cannot make
it fail — a banner that crashes the dashboard it is warning you about is worse
than no banner at all.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hd.config import Settings
from hd.dashboard.components import health
from hd.doctor import FAIL, OK, WARN, Check


@pytest.fixture(autouse=True)
def _clear_cache():
    health._cache = None
    yield
    health._cache = None


def _settings() -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:")


class TestCaching:
    async def test_second_call_does_not_re_run_the_checks(self):
        """The dashboard is resident; launchctl and the db are not free."""
        calls = []

        async def fake(settings):
            calls.append(1)
            return [Check("x", OK, "fine")]

        with patch.object(health, "run_checks", fake):
            await health._checks(_settings())
            await health._checks(_settings())
        assert len(calls) == 1

    async def test_force_bypasses_the_cache(self):
        """After a repair the banner has to re-ask rather than show stale state."""
        calls = []

        async def fake(settings):
            calls.append(1)
            return [Check("x", OK, "fine")]

        with patch.object(health, "run_checks", fake):
            await health._checks(_settings())
            await health._checks(_settings(), force=True)
        assert len(calls) == 2


class TestFailureIsContained:
    async def test_a_broken_check_does_not_propagate(self):
        """launchctl missing, a locked db — the page must still render."""

        async def boom(settings):
            raise RuntimeError("launchctl exploded")

        with patch.object(health, "run_checks", boom):
            assert await health._checks(_settings()) == []

    async def test_a_broken_check_is_not_cached_as_healthy(self):
        """Otherwise one transient failure hides real problems for the whole TTL."""

        async def boom(settings):
            raise RuntimeError("nope")

        with patch.object(health, "run_checks", boom):
            await health._checks(_settings())
        assert health._cache is None


class TestWhatCountsAsWorthShowing:
    def test_ok_checks_are_not_worth_a_banner(self):
        checks = [Check("a", OK, "fine"), Check("b", OK, "fine")]
        assert [c for c in checks if c.status in (FAIL, WARN)] == []

    def test_a_single_warn_is(self):
        checks = [Check("a", OK, "fine"), Check("b", WARN, "getting large")]
        assert len([c for c in checks if c.status in (FAIL, WARN)]) == 1


class TestNextScanLabel:
    """What replaced the "Scan now" button.

    The button promised an immediate result and delivered a 10-30 minute one,
    which for a first-time user reads as broken and invites repeat presses.
    People reached for it because nothing on the page said whether the scanner
    was still working — so the page says so instead.
    """

    def test_names_a_time(self):
        from hd.dashboard.pages.overview import _next_scan_label

        label = _next_scan_label()
        assert "next scan" in label
        assert ":" in label

    def test_says_tomorrow_after_the_last_slot(self):
        from datetime import datetime

        from hd.dashboard.pages import overview

        class _Late(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 21, 23, 59)

        with patch("datetime.datetime", _Late):
            assert "tomorrow" in overview._next_scan_label()

    def test_a_broken_schedule_lookup_yields_no_label(self):
        """Never let a decoration take the page down."""
        from hd.dashboard.pages import overview

        with patch("hd.setup_schedule.scan_slots", side_effect=RuntimeError("nope")):
            assert overview._next_scan_label() == ""


class TestBannerLevel:
    """A banner means "not collecting". Anything quieter must not produce one.

    Retiring a store whose price history you keep leaves `stray-store` warning
    permanently. Rendering that as a banner would light the dashboard forever
    for a condition chosen on purpose — and an always-on signal is the one
    nobody reads on the day the schedule actually dies.
    """

    def test_all_clear_is_quiet(self):
        assert health.banner_level([Check("a", OK, "fine")]) == "quiet"

    def test_no_checks_at_all_is_quiet(self):
        """run_checks returning [] means the checks failed, not that all is well."""
        assert health.banner_level([]) == "quiet"

    def test_a_warning_never_becomes_a_banner(self):
        checks = [Check("stray-store", WARN, "store 8452 holds data but is not scanned")]
        assert health.banner_level(checks) == "advisory"

    def test_many_warnings_still_never_become_a_banner(self):
        checks = [Check(f"w{i}", WARN, "hm") for i in range(6)]
        assert health.banner_level(checks) == "advisory"

    def test_one_failure_outranks_any_number_of_warnings(self):
        checks = [Check(f"w{i}", WARN, "hm") for i in range(6)]
        checks.append(Check("prune-job", FAIL, "no prune job installed"))
        assert health.banner_level(checks) == "fail"
