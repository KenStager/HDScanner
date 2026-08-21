"""Tests for scan liveness reporting.

The defect: on 2026-08-19 the scanner was blind from 20:00 until 12:00 the next
day. One HEALTH_DEGRADED row was written at the start, deduped for 24 hours,
and filtered out of Slack as "internal". Every later run logged "already exists
within 24h, skipping". The owner found out by asking.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hd.pipeline.health import (
    HealthStatus,
    ScanHealth,
    load_scan_health,
    next_scan_health,
    outage_duration_hours,
    save_scan_health,
)

T0 = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)


def test_first_failure_reports_a_transition():
    state, transition = next_scan_health(ScanHealth(), ok=False, now=T0)
    assert transition == "degraded"
    assert state.status is HealthStatus.DEGRADED
    assert state.consecutive_failures == 1


def test_a_sustained_outage_reports_once_not_every_run():
    """Six failed runs in a row must produce one notification, not six."""
    state, transitions = ScanHealth(), []
    for i in range(6):
        state, t = next_scan_health(state, ok=False, now=T0 + timedelta(hours=4 * i))
        transitions.append(t)
    assert transitions == ["degraded", None, None, None, None, None]
    assert state.consecutive_failures == 6


def test_recovery_is_announced():
    state, _ = next_scan_health(ScanHealth(), ok=False, now=T0)
    state, transition = next_scan_health(state, ok=True, now=T0 + timedelta(hours=16))
    assert transition == "recovered"
    assert state.status is HealthStatus.HEALTHY
    assert state.consecutive_failures == 0


def test_a_healthy_run_after_healthy_runs_says_nothing():
    """The common case: no news is no message."""
    state = ScanHealth()
    for i in range(5):
        state, t = next_scan_health(state, ok=True, now=T0 + timedelta(hours=i))
        assert t is None


def test_the_full_outage_produces_exactly_two_messages():
    """20:00 blind through 12:00 next day: 'stopped' and 'resumed'."""
    state, sent = ScanHealth(status=HealthStatus.HEALTHY, last_ok=T0.isoformat()), []
    for hours, ok in [(0, False), (4, False), (8, False), (11, False), (16, True)]:
        state, t = next_scan_health(state, ok=ok, now=T0 + timedelta(hours=hours))
        if t:
            sent.append(t)
    assert sent == ["degraded", "recovered"]


def test_outage_duration_is_measured_from_the_last_good_scan():
    state = ScanHealth(status=HealthStatus.DEGRADED, last_ok=T0.isoformat())
    assert outage_duration_hours(state, T0 + timedelta(hours=16)) == pytest.approx(16.0)


def test_duration_is_unknown_before_any_successful_scan():
    assert outage_duration_hours(ScanHealth(), T0) is None


def test_naive_last_ok_is_read_as_utc():
    state = ScanHealth(last_ok=T0.replace(tzinfo=None).isoformat())
    assert outage_duration_hours(state, T0 + timedelta(hours=2)) == pytest.approx(2.0)


# --- persistence ------------------------------------------------------------

def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "health"
    state, _ = next_scan_health(ScanHealth(), ok=False, now=T0)
    save_scan_health(path, state)

    # A fresh process, as the next scheduled run would be.
    reloaded = load_scan_health(path)
    assert reloaded.status is HealthStatus.DEGRADED
    assert reloaded.consecutive_failures == 1

    # ...and it must not re-announce what it already announced.
    _, transition = next_scan_health(reloaded, ok=False, now=T0 + timedelta(hours=4))
    assert transition is None


def test_missing_file_reads_as_healthy(tmp_path):
    assert load_scan_health(tmp_path / "nope").status is HealthStatus.HEALTHY


def test_corrupt_file_fails_open_rather_than_inventing_an_outage(tmp_path):
    path = tmp_path / "health"
    path.write_text("{not json")
    assert load_scan_health(path).status is HealthStatus.HEALTHY


def test_unknown_status_string_is_treated_as_healthy(tmp_path):
    path = tmp_path / "health"
    path.write_text(json.dumps({"status": "WHO_KNOWS"}))
    assert load_scan_health(path).status is HealthStatus.HEALTHY


def test_save_survives_an_unwritable_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    save_scan_health(blocker / "health", ScanHealth())  # must not raise
