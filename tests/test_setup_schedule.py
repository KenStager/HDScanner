"""Tests for generated schedules.

The bug being fixed: launchd's StartCalendarInterval fires on local time, but
the Daily Deals slot targets Home Depot's 3:00 Eastern refresh. The shipped
plist hardcoded 3:10, so anyone outside Eastern ran it hours late — three
hours, for a Pacific user, every day.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hd.setup_schedule import (
    ScheduleSlot,
    daily_deals_slot,
    hd_executable,
    label_for,
    prune_slot,
    render_crontab,
    render_prune_plist,
    render_scan_plist,
    scan_slots,
)

# A fixed date so DST is deterministic rather than "whenever the suite runs".
WINTER = datetime(2026, 1, 15)
SUMMER = datetime(2026, 7, 15)
EASTERN = ZoneInfo("America/New_York")
PACIFIC = ZoneInfo("America/Los_Angeles")
LONDON = ZoneInfo("Europe/London")


class TestDailyDealsSlot:
    def test_eastern_is_unchanged(self):
        assert daily_deals_slot(tz=EASTERN, on=WINTER) == ScheduleSlot(3, 10)

    def test_pacific_is_three_hours_earlier(self):
        """The actual bug: a hardcoded 3:10 ran three hours after the refresh."""
        assert daily_deals_slot(tz=PACIFIC, on=WINTER) == ScheduleSlot(0, 10)

    def test_london_is_later_the_same_day(self):
        assert daily_deals_slot(tz=LONDON, on=WINTER) == ScheduleSlot(8, 10)

    def test_holds_across_dst(self):
        """US zones shift together, so the local time is stable year round."""
        assert daily_deals_slot(tz=PACIFIC, on=WINTER) == daily_deals_slot(
            tz=PACIFIC, on=SUMMER
        )

    def test_grace_period_is_after_the_refresh(self):
        slot = daily_deals_slot(tz=EASTERN, on=WINTER)
        assert (slot.hour, slot.minute) > (3, 0)


class TestScanSlots:
    def test_includes_the_deals_slot(self):
        slots = scan_slots(tz=EASTERN, on=WINTER)
        assert ScheduleSlot(3, 10) in slots

    def test_sorted_and_deduplicated(self):
        slots = scan_slots(tz=EASTERN, on=WINTER)
        keys = [(s.hour, s.minute) for s in slots]
        assert keys == sorted(keys)
        assert len(keys) == len(set(keys))

    def test_shifted_zone_still_has_every_slot(self):
        assert len(scan_slots(tz=PACIFIC, on=WINTER)) == len(scan_slots(tz=EASTERN, on=WINTER))


class TestRenderPlists:
    WORK = Path("/Users/someone/HDScanner")
    HD = Path("/Users/someone/HDScanner/.venv/bin/hd")

    def test_scan_plist_uses_the_given_paths(self):
        out = render_scan_plist("com.someone.hdscanner", self.WORK, self.HD,
                                [ScheduleSlot(3, 10)])
        assert "/Users/someone/HDScanner" in out
        assert "com.someone.hdscanner" in out
        assert "/Users/kstager" not in out

    def test_scan_plist_has_no_hardcoded_owner(self):
        """The shipped plists hardcoded one user's home across six lines."""
        out = render_scan_plist(label_for(user="alice"), self.WORK, self.HD, [ScheduleSlot(0, 0)])
        assert "com.alice.hdscanner" in out

    def test_scan_plist_encodes_every_slot(self):
        slots = [ScheduleSlot(0, 0), ScheduleSlot(3, 10), ScheduleSlot(20, 0)]
        out = render_scan_plist("l", self.WORK, self.HD, slots)
        for s in slots:
            assert f"<integer>{s.hour}</integer>" in out
        assert out.count("<key>Hour</key>") == 3

    def test_scan_plist_runs_notify_after_the_scan(self):
        out = render_scan_plist("l", self.WORK, self.HD, [ScheduleSlot(0, 0)])
        assert "run-once" in out and "notify" in out
        assert out.index("run-once") < out.index("notify")

    def test_prune_is_a_separate_job(self):
        """The shipped schedule never pruned, which is why the db reached 1.4 GB."""
        out = render_prune_plist("l.prune", self.WORK, self.HD, ScheduleSlot(4, 30))
        assert "prune" in out
        assert "run-once" not in out

    def test_plists_are_well_formed_xml(self):
        import xml.etree.ElementTree as ET

        for text in (
            render_scan_plist("l", self.WORK, self.HD, [ScheduleSlot(3, 10), ScheduleSlot(8, 0)]),
            render_prune_plist("l.prune", self.WORK, self.HD, ScheduleSlot(4, 30)),
        ):
            ET.fromstring(text)  # raises if malformed

    def test_ampersands_are_escaped(self):
        out = render_scan_plist("l", self.WORK, self.HD, [ScheduleSlot(0, 0)])
        assert "&amp;&amp;" in out
        assert " && " not in out


class TestRenderCrontab:
    def test_one_line_per_slot_plus_prune(self):
        out = render_crontab(
            Path("/srv/hd"), Path("/srv/hd/.venv/bin/hd"),
            [ScheduleSlot(0, 0), ScheduleSlot(3, 10)], ScheduleSlot(4, 30),
        )
        rows = [l for l in out.splitlines() if l and not l.startswith("#")]
        assert len(rows) == 3
        assert "10 3 * * *" in out
        assert "30 4 * * * cd /srv/hd && /srv/hd/.venv/bin/hd prune" in out


class TestLabelAndExecutable:
    def test_label_is_namespaced_to_the_user(self):
        assert label_for(user="bob") == "com.bob.hdscanner"

    def test_label_falls_back_without_a_user(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        assert label_for() == "com.local.hdscanner"

    def test_hd_executable_sits_beside_the_interpreter(self):
        assert hd_executable().name == "hd"
        assert hd_executable().is_absolute()
