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
    quietest_hour_et,
    render_crontab,
    render_prune_plist,
    render_dashboard_plist,
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
    def test_no_separate_deals_slot_when_a_scan_already_covers_it(self):
        """04:00 ET starts an hour after the refresh and checks deals first."""
        slots = scan_slots(tz=EASTERN, on=WINTER, hours_et=(4, 12, 20), minute=0)
        assert ScheduleSlot(3, 10) not in slots
        assert ScheduleSlot(4, 0) in slots

    def test_deals_slot_returns_when_no_scan_lands_near_the_refresh(self):
        from hd.setup_schedule import deals_slot_needed

        assert deals_slot_needed((0, 8, 12, 16, 20)) is True   # nothing near 03:00
        assert deals_slot_needed((0, 4, 8, 12, 16, 20)) is False
        assert deals_slot_needed((0, 3, 8)) is False           # exactly on the refresh
        assert deals_slot_needed((0, 6, 8)) is True            # three hours later is too late

    def test_sorted_and_deduplicated(self):
        slots = scan_slots(tz=EASTERN, on=WINTER)
        keys = [(s.hour, s.minute) for s in slots]
        assert keys == sorted(keys)
        assert len(keys) == len(set(keys))

    def test_shifted_zone_still_has_every_slot(self):
        assert len(scan_slots(tz=PACIFIC, on=WINTER)) == len(scan_slots(tz=EASTERN, on=WINTER))


class TestMaintenanceSlot:
    """Maintenance must not overlap a scan: pruning deletes rows and may VACUUM,
    which takes an exclusive lock. It used to sit at 04:30 — thirty minutes into
    what is now the longest run of the day."""

    def test_lands_in_the_middle_of_the_gap(self):
        assert quietest_hour_et((0, 4, 8, 12, 16, 20)) == 2

    def test_reproduces_the_old_hardcoded_hour_for_the_old_schedule(self):
        """04:30 was not arbitrary — it was the quietest hour before 04:00 existed."""
        assert quietest_hour_et((0, 8, 12, 16, 20)) == 4

    def test_prefers_the_widest_gap(self):
        # Gaps are 0->1, 1->2, 2->14 (twelve hours) and 14->0 (ten). The
        # widest is 2->14, whose midpoint is 08:00.
        assert quietest_hour_et((0, 1, 2, 14)) == 8

    def test_wraps_around_midnight(self):
        # 2->22 is twenty hours; 22->2 is four. Midpoint of the former is noon.
        assert quietest_hour_et((22, 2)) == 12

    def test_single_scan_goes_opposite(self):
        assert quietest_hour_et((6,)) == 18

    def test_empty_falls_back(self):
        from hd.setup_schedule import PRUNE_HOUR_ET

        assert quietest_hour_et(()) == PRUNE_HOUR_ET

    def test_slot_is_half_past(self):
        slot = prune_slot(tz=EASTERN, on=WINTER, hours_et=(0, 4, 8, 12, 16, 20))
        assert (slot.hour, slot.minute) == (2, 30)

    def test_slot_follows_the_cadence_actually_configured(self):
        """Pruning takes an exclusive lock; it must not land inside a scan."""
        slot = prune_slot(tz=EASTERN, on=WINTER, hours_et=(4, 12, 20))
        assert (slot.hour, slot.minute) == (8, 30)

    def test_slot_never_collides_with_a_scan(self):
        from hd.setup_schedule import scan_slots

        maintenance = prune_slot(tz=EASTERN, on=WINTER)
        scans = {(s.hour, s.minute) for s in scan_slots(tz=EASTERN, on=WINTER)}
        assert (maintenance.hour, maintenance.minute) not in scans
        # and at least an hour clear of the nearest scan start
        assert min(abs(maintenance.hour - h) for h, _ in scans) >= 1


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

    def test_dashboard_job_is_resident(self):
        """The scan and prune jobs fire and exit; this one has to stay up.

        Without KeepAlive the dashboard lives only as long as the terminal that
        started it, which is the whole reason an install needed a terminal
        after day one.
        """
        out = render_dashboard_plist("l.dashboard", self.WORK, self.HD)
        assert "<key>KeepAlive</key>\n  <true/>" in out
        assert "<key>RunAtLoad</key>\n  <true/>" in out
        assert "serve" in out
        assert "run-once" not in out and "prune" not in out

    def test_dashboard_job_throttles_a_crash_loop(self):
        """A missing dashboard extra or an occupied port must not spin."""
        out = render_dashboard_plist("l.dashboard", self.WORK, self.HD)
        assert "<key>ThrottleInterval</key>" in out

    def test_dashboard_job_does_not_bake_in_host_or_port(self):
        """`hd serve` reads them from settings, so .env stays the one source."""
        out = render_dashboard_plist("l.dashboard", self.WORK, self.HD)
        assert "8080" not in out
        assert "127.0.0.1" not in out

    def test_plists_are_well_formed_xml(self):
        import xml.etree.ElementTree as ET

        for text in (
            render_scan_plist("l", self.WORK, self.HD, [ScheduleSlot(3, 10), ScheduleSlot(8, 0)]),
            render_prune_plist("l.prune", self.WORK, self.HD, ScheduleSlot(4, 30)),
            render_dashboard_plist("l.dashboard", self.WORK, self.HD),
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
        rows = [l for l in out.splitlines() if l and not l.startswith("#") and "*" in l]
        assert len(rows) == 3
        assert "10 3 * * *" in out
        assert "30 4 * * * cd /srv/hd && /srv/hd/.venv/bin/hd prune" in out

    def test_silences_cron_mail(self):
        """Six runs a day would otherwise mail the user six times."""
        out = render_crontab(Path("/srv/hd"), Path("/srv/hd/.venv/bin/hd"),
                             [ScheduleSlot(0, 0)], ScheduleSlot(4, 30))
        assert 'MAILTO=""' in out


class TestLabelAndExecutable:
    def test_label_is_namespaced_to_the_user(self):
        assert label_for(user="bob") == "com.bob.hdscanner"

    def test_label_falls_back_without_a_user(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        assert label_for() == "com.local.hdscanner"

    def test_hd_executable_sits_beside_the_interpreter(self):
        assert hd_executable().name == "hd"
        assert hd_executable().is_absolute()


class TestHostilePaths:
    """A home directory with a space or an ampersand is ordinary on macOS.

    Unquoted, `cd /Users/bob/My Projects` fails with "too many arguments" and
    the && chain stops — the scanner simply never runs, at 4am, silently. An
    unescaped ampersand produces a plist launchd refuses to load.
    """

    SPACED = Path("/Users/bob/My Projects/HDScanner")
    NASTY = Path("/Users/bob/Home & Garden/HD <Scanner>")
    HD = Path("/Users/bob/My Projects/.venv/bin/hd")

    def _command(self, text: str) -> str:
        import plistlib

        return plistlib.loads(text.encode())["ProgramArguments"][2]

    def test_scan_plist_parses_with_metacharacters(self):
        import plistlib

        text = render_scan_plist("com.bob.hd&co", self.NASTY, self.HD, [ScheduleSlot(3, 10)])
        parsed = plistlib.loads(text.encode())
        assert parsed["Label"] == "com.bob.hd&co"
        assert parsed["WorkingDirectory"] == str(self.NASTY)

    def test_prune_plist_parses_with_metacharacters(self):
        import plistlib

        text = render_prune_plist("com.bob.hd.prune", self.NASTY, self.HD, ScheduleSlot(4, 30))
        assert plistlib.loads(text.encode())["WorkingDirectory"] == str(self.NASTY)

    def test_spaces_are_shell_quoted(self):
        cmd = self._command(render_scan_plist("l", self.SPACED, self.HD, [ScheduleSlot(0, 0)]))
        assert "'/Users/bob/My Projects/HDScanner'" in cmd
        assert "cd /Users/bob/My Projects/HDScanner &&" not in cmd

    def test_crontab_quotes_paths(self):
        out = render_crontab(self.SPACED, self.HD, [ScheduleSlot(0, 0)], ScheduleSlot(4, 30))
        assert "'/Users/bob/My Projects/HDScanner'" in out


class TestDealsSlotCarry:
    def test_grace_period_never_produces_an_invalid_minute(self):
        """divmod, not raw addition — 3:55 + 10 must roll the hour."""
        import hd.setup_schedule as sched

        original = sched.HD_DEALS_REFRESH
        try:
            sched.HD_DEALS_REFRESH = sched.dtime(3, 55)
            slot = daily_deals_slot(tz=EASTERN, on=WINTER)
            assert slot == ScheduleSlot(4, 5)
        finally:
            sched.HD_DEALS_REFRESH = original


class TestExecutableDiscovery:
    def test_missing_executable_returns_none(self, monkeypatch, tmp_path):
        """Better to warn than to write a job that can never run.

        Under pipx, uv tool or `python -m hd` there is no `hd` beside the
        interpreter, and a plist naming a nonexistent binary fails silently at
        every scheduled time.
        """
        import hd.setup_schedule as sched

        monkeypatch.setattr(sched.sys, "executable", str(tmp_path / "bin" / "python"))
        monkeypatch.setattr(sched.shutil, "which", lambda name: None)
        assert hd_executable() is None

    def test_falls_back_to_path_lookup(self, monkeypatch, tmp_path):
        import hd.setup_schedule as sched

        monkeypatch.setattr(sched.sys, "executable", str(tmp_path / "bin" / "python"))
        monkeypatch.setattr(sched.shutil, "which", lambda name: "/usr/local/bin/hd")
        assert hd_executable() == Path("/usr/local/bin/hd")


class TestCadence:
    """Three a day, on a minute that is this install's own.

    Nine of the last ten runs on the author's install ended on an HTTP 206
    quota stop — the per-install limit already binding at one install watching
    one store. The limit that actually matters is the one across every install,
    and that one does not aggregate: the shipped default is the only lever.
    """

    def test_ships_three_scans_a_day(self):
        from hd.setup_schedule import SCAN_HOURS_ET

        assert len(SCAN_HOURS_ET) == 3

    def test_keeps_both_full_shelf_walks(self):
        """04:00 and 12:00 are the full-shelf hours; a thinner cadence must keep them."""
        from hd.setup_schedule import SCAN_HOURS_ET

        assert 4 in SCAN_HOURS_ET and 12 in SCAN_HOURS_ET

    def test_scans_are_evenly_spaced(self):
        from hd.setup_schedule import SCAN_HOURS_ET

        hours = sorted(SCAN_HOURS_ET)
        gaps = {b - a for a, b in zip(hours, hours[1:])}
        assert gaps == {8}

    def test_the_default_needs_no_extra_deals_slot(self):
        """A fourth run for the deals page would undo a third of the saving."""
        from hd.setup_schedule import SCAN_HOURS_ET, deals_slot_needed

        assert not deals_slot_needed(SCAN_HOURS_ET)

    def test_two_installs_do_not_share_a_minute(self):
        """A fixed :15 offset moves the crowd; it does not disperse it."""
        from pathlib import Path

        from hd.setup_schedule import scan_minute

        minutes = {scan_minute(Path(f"/Users/u{i}/HDScanner")) for i in range(12)}
        assert len(minutes) > 6, f"only {len(minutes)} distinct minutes across 12 installs"

    def test_an_install_keeps_its_minute(self):
        """It goes into a plist; a minute that drifts would rewrite the schedule."""
        from pathlib import Path

        from hd.setup_schedule import scan_minute

        here = Path("/Users/someone/HDScanner")
        assert scan_minute(here) == scan_minute(here)

    def test_minute_is_a_real_minute(self):
        from pathlib import Path

        from hd.setup_schedule import scan_minute

        for i in range(50):
            assert 0 <= scan_minute(Path(f"/x/{i}")) < 60
