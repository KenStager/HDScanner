"""Tests for the installation checks.

These target the gap the test suite had: defects that live between the code and
the machine it runs on, which no amount of unit testing of the pipeline catches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hd.config import Settings
from hd.doctor import (
    FAIL,
    OK,
    WARN,
    _check_interpreter,
    check_cooldown,
    check_identity,
    check_liveness,
    check_storage,
)


def settings_for(tmp_path, **kw):
    base = dict(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db",
        health_state_path=str(tmp_path / "health"),
        throttle_cooldown_path=str(tmp_path / "cool"),
        raw_json_dir=str(tmp_path / "raw"),
        stores="2619",
    )
    base.update(kw)
    return Settings(**base)


def statuses(checks):
    return [c.status for c in checks]


# --- the bug that shipped today --------------------------------------------

def test_bare_hd_in_the_plist_is_a_failure():
    """A bare `hd` resolved to an unrelated interpreter and failed silently."""
    data = {"ProgramArguments": ["/bin/bash", "-lc", "cd /x && hd run-once && hd notify"]}
    checks = list(_check_interpreter(data, Path("scan.plist")))
    assert all(c.status == FAIL for c in checks)
    assert "bare `hd`" in checks[0].detail


def test_venv_path_passes(tmp_path):
    hd = tmp_path / ".venv" / "bin" / "hd"
    hd.parent.mkdir(parents=True)
    hd.write_text("#!/bin/sh\n")
    data = {"ProgramArguments": ["/bin/bash", "-lc", f"cd /x && {hd} run-once"]}
    assert statuses(_check_interpreter(data, Path("scan.plist"))) == [OK]


def test_missing_interpreter_is_a_failure(tmp_path):
    ghost = tmp_path / ".venv" / "bin" / "hd"
    data = {"ProgramArguments": ["/bin/bash", "-lc", f"cd /x && {ghost} run-once"]}
    checks = list(_check_interpreter(data, Path("scan.plist")))
    assert checks[0].status == FAIL
    assert "does not exist" in checks[0].detail


def test_interpreter_outside_the_venv_warns(tmp_path):
    other = tmp_path / "elsewhere" / "hd"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    data = {"ProgramArguments": ["/bin/bash", "-lc", f"cd /x && {other} run-once"]}
    assert statuses(_check_interpreter(data, Path("scan.plist"))) == [WARN]


# --- liveness ---------------------------------------------------------------

def test_degraded_scanner_is_a_failure(tmp_path):
    from hd.pipeline.health import HealthStatus, ScanHealth, save_scan_health

    s = settings_for(tmp_path)
    save_scan_health(s.health_state_path, ScanHealth(
        status=HealthStatus.DEGRADED,
        last_ok=(datetime.now(timezone.utc) - timedelta(hours=16)).isoformat(),
        consecutive_failures=4,
    ))
    check = list(check_liveness(s))[0]
    assert check.status == FAIL
    assert "16h" in check.detail and "4 consecutive" in check.detail


def test_recent_success_passes(tmp_path):
    from hd.pipeline.health import ScanHealth, save_scan_health

    s = settings_for(tmp_path)
    save_scan_health(s.health_state_path, ScanHealth(
        last_ok=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))
    assert list(check_liveness(s))[0].status == OK


def test_long_silence_warns_even_when_nominally_healthy(tmp_path):
    """A healthy flag with no scan for a day is still worth surfacing."""
    from hd.pipeline.health import ScanHealth, save_scan_health

    s = settings_for(tmp_path)
    save_scan_health(s.health_state_path, ScanHealth(
        last_ok=(datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()))
    assert list(check_liveness(s))[0].status == WARN


# --- the rest ---------------------------------------------------------------

def test_cooldown_is_surfaced(tmp_path):
    from hd.http.cooldown import ThrottleCooldown

    s = settings_for(tmp_path)
    ThrottleCooldown(s.throttle_cooldown_path).start(1800)
    check = list(check_cooldown(s))[0]
    assert check.status == WARN and "min" in check.detail


def test_missing_contact_address_warns(tmp_path):
    assert list(check_identity(settings_for(tmp_path, contact_email="")))[0].status == WARN
    assert list(check_identity(settings_for(tmp_path, contact_email="a@b.c")))[0].status == OK


def test_raw_response_directory_is_measured(tmp_path):
    s = settings_for(tmp_path)
    raw = Path(s.raw_json_dir)
    raw.mkdir()
    for i in range(3):
        (raw / f"r{i}.json").write_text("{}")
    check = list(check_storage(s))[0]
    assert check.status == OK and "3 files" in check.detail


@pytest.mark.asyncio
async def test_database_check_reports_retention_debt_and_stray_stores(tmp_path):
    from hd.db import base
    from hd.db.models import StoreSnapshot
    from hd.doctor import check_database

    s = settings_for(tmp_path, snapshot_retention_days=90, stores="2619")
    await base.init_db(s)
    now = datetime.now(timezone.utc)
    async with base.get_session(s) as session:
        for i in range(4):   # old rows for a store we no longer scan
            session.add(StoreSnapshot(ts=now - timedelta(days=200), store_id="8425",
                                      item_id=f"o{i}", price_value=1))
        session.add(StoreSnapshot(ts=now, store_id="2619", item_id="n1", price_value=1))
        await session.commit()

    checks = await check_database(s)
    await base.close_db()
    by_name = {c.name: c for c in checks}
    assert by_name["retention"].status == WARN
    assert "80%" in by_name["retention"].detail
    assert by_name["stray-store"].status == WARN
    assert "8425" in by_name["stray-store"].detail


@pytest.mark.asyncio
async def test_a_retired_store_is_an_answer_not_a_warning(tmp_path):
    """Dropping a store while keeping its price history is a normal thing to do.

    Without a way to say so, `stray-store` warns forever: the dashboard shows a
    standing advisory for a state chosen on purpose, and a signal that never
    clears is the one nobody reads on the day something is genuinely wrong.
    """
    from hd.db import base
    from hd.db.models import StoreSnapshot
    from hd.doctor import check_database

    s = settings_for(tmp_path, snapshot_retention_days=90,
                     stores="2619", retired_stores="8425")
    await base.init_db(s)
    now = datetime.now(timezone.utc)
    async with base.get_session(s) as session:
        session.add(StoreSnapshot(ts=now, store_id="8425", item_id="o1", price_value=1))
        session.add(StoreSnapshot(ts=now, store_id="2619", item_id="n1", price_value=1))
        await session.commit()

    checks = await check_database(s)
    await base.close_db()
    assert "stray-store" not in {c.name for c in checks}


@pytest.mark.asyncio
async def test_an_unretired_stray_store_says_how_to_settle_it(tmp_path):
    """The warning has to be resolvable, or it is just noise."""
    from hd.db import base
    from hd.db.models import StoreSnapshot
    from hd.doctor import check_database

    s = settings_for(tmp_path, snapshot_retention_days=90, stores="2619")
    await base.init_db(s)
    now = datetime.now(timezone.utc)
    async with base.get_session(s) as session:
        session.add(StoreSnapshot(ts=now, store_id="8425", item_id="o1", price_value=1))
        await session.commit()

    checks = await check_database(s)
    await base.close_db()
    stray = next(c for c in checks if c.name == "stray-store")
    assert "RETIRED_STORES" in (stray.fix or "")


# --- walk headroom: the ceiling transition nothing else reports -------------

async def _record_walks(s, rows):
    """rows: (tier, label, items_expected, minutes_ago)."""
    from hd.db import base
    from hd.db.models import WalkCoverage

    await base.init_db(s)
    now = datetime.now(timezone.utc)
    async with base.get_session(s) as session:
        for i, (tier, label, expected, ago) in enumerate(rows):
            started = now - timedelta(minutes=ago)
            session.add(WalkCoverage(
                run_id=1, store_id="2619", tier=tier, label=label,
                started=started, ended=started, status="complete",
                items_expected=expected, items_observed=expected or 0,
            ))
        await session.commit()


def _headroom_settings(tmp_path, **kw):
    # reachable cap = 720 + 24 = 744; band = 2*744 - 3*24 = 1416
    return settings_for(
        tmp_path, page_size=24, api_max_start_index=720,
        both_ends_paging=True, both_ends_min_overlap_pages=3, **kw,
    )


@pytest.mark.asyncio
async def test_shelf_near_the_both_ends_band_is_warned(tmp_path):
    """The live case: the in-store shelf at 1,367 of a 1,416 band.

    49 items of margin, against a 72-item (3-page) threshold. When it crosses,
    the shelf stops being one both-ends walk and becomes a facet split — which
    is exactly the thing that would break a one-run in-store mandate, silently.
    """
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await _record_walks(s, [("IN_STORE", "MILWAUKEE", 1367, 5)])
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert [c.status for c in checks] == [WARN]
    assert "MILWAUKEE" in checks[0].detail
    assert "1,367 of 1,416" in checks[0].detail
    # 1,367 of 1,416: it takes 50 more items to exceed the band, not 49.
    assert "50 more item(s)" in checks[0].detail
    assert "facet split" in checks[0].detail


@pytest.mark.asyncio
async def test_comfortable_node_is_not_warned(tmp_path):
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await _record_walks(s, [("ALL", "MILWAUKEE/Drills", 389, 5)])
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert [c.status for c in checks] == [OK]


@pytest.mark.asyncio
async def test_single_walk_near_reachable_cap_names_both_ends_not_a_split(tmp_path):
    """Past reachable_cap a walk becomes a both-ends PAIR, not a facet split.

    Reporting it as a split would send the operator looking for a cost that
    isn't there — both-ends is the cheap route, roughly double one walk.
    """
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await _record_walks(s, [("ALL", "MILWAUKEE/Special Values", 678, 5)])
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert [c.status for c in checks] == [WARN]
    assert "678 of 744" in checks[0].detail
    assert "67 more item(s)" in checks[0].detail
    assert "both-ends pair" in checks[0].detail
    assert "facet split" not in checks[0].detail


@pytest.mark.asyncio
async def test_node_already_past_the_band_is_silent(tmp_path):
    """Already split is a settled state, not a warning — the cliff is behind it."""
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await _record_walks(s, [("ALL", "MILWAUKEE/Outdoors", 9000, 5)])
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert [c.status for c in checks] == [OK]


@pytest.mark.asyncio
async def test_only_the_latest_measurement_of_a_node_counts(tmp_path):
    """A node that has since shrunk must not warn on a stale high-water row."""
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await _record_walks(s, [
        ("IN_STORE", "MILWAUKEE", 1400, 600),   # older: would warn
        ("IN_STORE", "MILWAUKEE", 900, 5),      # newest: comfortable
    ])
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert [c.status for c in checks] == [OK]


@pytest.mark.asyncio
async def test_no_coverage_yet_is_not_a_warning(tmp_path):
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await base.init_db(s)
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert [c.status for c in checks] == [OK]
    assert "no walk coverage" in checks[0].detail


@pytest.mark.asyncio
async def test_headroom_without_both_ends_names_the_split_and_the_remedy(tmp_path):
    """The path every fresh clone takes — both_ends_paging ships False.

    With no band, a node near reachable_cap goes straight to a facet split,
    and the useful advice is to enable the cheaper route rather than to budget
    for extra walks.
    """
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = settings_for(tmp_path, page_size=24, api_max_start_index=720,
                     both_ends_paging=False, both_ends_min_overlap_pages=3)
    await _record_walks(s, [("ALL", "MILWAUKEE/Special Values", 700, 5)])
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert [c.status for c in checks] == [WARN]
    assert "700 of 744" in checks[0].detail
    assert "facet split" in checks[0].detail
    assert "both-ends pair" not in checks[0].detail
    assert "BOTH_ENDS_PAGING" in (checks[0].fix or "")


@pytest.mark.asyncio
async def test_headroom_fix_advice_widens_the_band_rather_than_narrowing_it(tmp_path):
    """band = 2*reachable_cap - overlap*page_size, so RAISING overlap narrows it.

    Advising an operator to raise it would push the very node being warned
    about straight over the edge. The fix text is operator-facing and acted on,
    so it is asserted, not just the detail.
    """
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await _record_walks(s, [("IN_STORE", "MILWAUKEE", 1367, 5)])
    checks = await check_walk_headroom(s)
    await base.close_db()

    fix = checks[0].fix or ""
    assert "lower BOTH_ENDS_MIN_OVERLAP_PAGES" in fix
    assert "raise BOTH_ENDS_MIN_OVERLAP_PAGES" not in fix


@pytest.mark.asyncio
async def test_headroom_sees_a_node_that_has_not_been_walked_recently(tmp_path):
    """No row limit: a node on a slow rotation must not fall out of the window.

    An "ORDER BY started DESC LIMIT n" would drop the stale node entirely and
    then report an affirmative all-clear over a set it had silently truncated.
    """
    from hd.db import base
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    rows = [("ALL", f"filler/{i}", 10, 1) for i in range(600)]
    rows.append(("IN_STORE", "MILWAUKEE", 1367, 5000))   # old, and at the cliff
    await _record_walks(s, rows)
    checks = await check_walk_headroom(s)
    await base.close_db()

    assert any("MILWAUKEE" in c.detail for c in checks if c.status == WARN)


@pytest.mark.asyncio
async def test_headroom_keeps_stores_apart(tmp_path):
    """Two stores walk the same labels; one must not mask the other."""
    from hd.db import base
    from hd.db.models import WalkCoverage
    from hd.doctor import check_walk_headroom

    s = _headroom_settings(tmp_path)
    await base.init_db(s)
    now = datetime.now(timezone.utc)
    async with base.get_session(s) as session:
        for store, expected, ago in (("2619", 1367, 5), ("8452", 100, 1)):
            session.add(WalkCoverage(
                run_id=1, store_id=store, tier="IN_STORE", label="MILWAUKEE",
                started=now - timedelta(minutes=ago), ended=now,
                status="complete", items_expected=expected, items_observed=expected,
            ))
        await session.commit()
    checks = await check_walk_headroom(s)
    await base.close_db()

    warns = [c for c in checks if c.status == WARN]
    assert len(warns) == 1
    assert "store 2619" in warns[0].detail


@pytest.mark.asyncio
async def test_headroom_is_registered_in_run_checks(tmp_path):
    """A check nothing calls is a check that does not exist."""
    from hd.db import base
    from hd.doctor import run_checks

    s = _headroom_settings(tmp_path)
    await _record_walks(s, [("IN_STORE", "MILWAUKEE", 1367, 5)])
    checks = await run_checks(s)
    await base.close_db()

    assert any(c.name == "walk-headroom" for c in checks)


# --- scan health -------------------------------------------------------------

async def _record_runs(s, rows):
    """rows: (run_id, hours_ago, status)."""
    from hd.db import base
    from hd.db.models import ScanRun

    await base.init_db(s)
    now = datetime.now(timezone.utc)
    async with base.get_session(s) as session:
        for run_id, ago, status in rows:
            started = now - timedelta(hours=ago)
            session.add(ScanRun(
                id=run_id, started=started,
                ended=None if status == "running" else started,
                tiers="network", status=status,
                walks=1, snapshots=1, requests_used=10,
            ))
        await session.commit()


@pytest.mark.asyncio
async def test_scan_health_fresh_runs_pass(tmp_path):
    from hd.db import base
    from hd.doctor import check_scan_health

    s = settings_for(tmp_path, scan_hours_et="0,4,8,12,16,20")
    await _record_runs(s, [(1, 6, "complete"), (2, 2, "complete")])
    checks = await check_scan_health(s)
    await base.close_db()

    assert statuses(checks) == [OK]


@pytest.mark.asyncio
async def test_scan_health_crashed_run_is_warned(tmp_path):
    """A hard crash leaves status='running' forever — no heartbeat, no
    finalizer. This check is the only place that ever says so."""
    from hd.db import base
    from hd.doctor import check_scan_health

    s = settings_for(tmp_path, scan_hours_et="0,4,8,12,16,20")
    await _record_runs(s, [(1, 3, "running"), (2, 2, "complete")])
    checks = await check_scan_health(s)
    await base.close_db()

    assert statuses(checks) == [WARN]
    assert "stuck at 'running'" in checks[0].detail


@pytest.mark.asyncio
async def test_scan_health_in_flight_run_is_not_a_crash(tmp_path):
    from hd.db import base
    from hd.doctor import check_scan_health

    s = settings_for(tmp_path, scan_hours_et="0,4,8,12,16,20")
    await _record_runs(s, [(1, 2, "complete"), (2, 0.2, "running")])
    checks = await check_scan_health(s)
    await base.close_db()

    assert statuses(checks) == [OK]


@pytest.mark.asyncio
async def test_scan_health_stall_threshold_follows_the_schedule(tmp_path):
    """A 9h-old newest run is a stall on a six-a-day schedule (4h gaps,
    8h threshold) and healthy on the shipped three-a-day one (8h gaps,
    16h threshold). The check must not hold one install to the other's clock."""
    from hd.db import base
    from hd.doctor import check_scan_health

    s6 = settings_for(tmp_path, scan_hours_et="0,4,8,12,16,20")
    await _record_runs(s6, [(1, 9, "complete")])
    checks = await check_scan_health(s6)
    await base.close_db()
    assert statuses(checks) == [WARN]
    assert "not advancing" in checks[0].detail

    (tmp_path / "b").mkdir()
    s3 = settings_for(tmp_path / "b", scan_hours_et="4,12,20")
    await _record_runs(s3, [(1, 9, "complete")])
    checks = await check_scan_health(s3)
    await base.close_db()
    assert statuses(checks) == [OK]


@pytest.mark.asyncio
async def test_scan_health_aborted_streak_is_warned(tmp_path):
    from hd.db import base
    from hd.doctor import check_scan_health

    s = settings_for(tmp_path, scan_hours_et="0,4,8,12,16,20")
    await _record_runs(s, [(1, 16, "complete"), (2, 12, "aborted"),
                           (3, 8, "aborted"), (4, 4, "aborted")])
    checks = await check_scan_health(s)
    await base.close_db()

    details = " | ".join(c.detail for c in checks)
    assert any(c.status == WARN and "3 finished runs all aborted" in c.detail
               for c in checks), details


@pytest.mark.asyncio
async def test_scan_health_empty_table_is_ok(tmp_path):
    from hd.db import base
    from hd.doctor import check_scan_health

    s = settings_for(tmp_path)
    from hd.db import base as b
    await b.init_db(s)
    checks = await check_scan_health(s)
    await base.close_db()

    assert statuses(checks) == [OK]
    assert "no scan runs recorded yet" in checks[0].detail


@pytest.mark.asyncio
async def test_scan_health_is_registered_in_run_checks(tmp_path):
    """A check nothing calls is a check that does not exist."""
    from hd.db import base
    from hd.doctor import run_checks

    s = settings_for(tmp_path)
    await _record_runs(s, [(1, 2, "complete")])
    checks = await run_checks(s)
    await base.close_db()

    assert any(c.name == "scan-health" for c in checks)
