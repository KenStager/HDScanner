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
