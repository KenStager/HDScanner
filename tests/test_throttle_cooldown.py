"""Tests for the throttle cooldown that survives process exit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hd.http.cooldown import ThrottleCooldown
from hd.hd_api.graphql import failure_reason

from tests.test_http_client import make_client, make_settings, resp, OK_BODY


def test_absent_file_means_no_cooldown(tmp_path):
    cd = ThrottleCooldown(tmp_path / "cool")
    assert cd.is_active() is False
    assert cd.remaining_seconds() == 0.0


def test_start_puts_a_cooldown_in_force(tmp_path):
    cd = ThrottleCooldown(tmp_path / "cool")
    cd.start(600)
    assert cd.is_active() is True
    assert 500 < cd.remaining_seconds() <= 600


def test_expired_cooldown_is_not_active(tmp_path):
    path = tmp_path / "cool"
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    path.write_text(past.isoformat())
    assert ThrottleCooldown(path).is_active() is False


def test_naive_timestamp_is_read_as_utc(tmp_path):
    path = tmp_path / "cool"
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    path.write_text(future.replace(tzinfo=None).isoformat())
    assert ThrottleCooldown(path).is_active() is True


def test_corrupt_file_fails_open(tmp_path):
    # A damaged cooldown file must not disable the scanner permanently; live
    # 206 handling still stops a run the moment it is throttled.
    path = tmp_path / "cool"
    path.write_text("not a timestamp")
    assert ThrottleCooldown(path).is_active() is False


def test_start_extends_but_never_shortens(tmp_path):
    cd = ThrottleCooldown(tmp_path / "cool")
    long_until = cd.start(3600)
    # A shorter second signal must not release us early.
    assert cd.start(60) == long_until
    assert cd.remaining_seconds() > 3000
    # A longer one does extend.
    assert cd.start(7200) > long_until


def test_clear_removes_the_cooldown(tmp_path):
    cd = ThrottleCooldown(tmp_path / "cool")
    cd.start(600)
    cd.clear()
    assert cd.is_active() is False
    cd.clear()  # idempotent


def test_default_duration_is_used_when_none_given(tmp_path):
    cd = ThrottleCooldown(tmp_path / "cool", default_seconds=120)
    cd.start()
    assert 60 < cd.remaining_seconds() <= 120


# --- client integration -----------------------------------------------------

@pytest.mark.asyncio
async def test_206_writes_a_cooldown_for_the_next_run(tmp_path):
    settings = make_settings(throttle_cooldown_path=str(tmp_path / "cool"))
    c = make_client([resp(206, json=OK_BODY)], settings)
    raw = await c.post_graphql({})
    await c.close()

    assert failure_reason(raw) == "http_206_quota"
    # A fresh client — as the next scheduled run would build — sees it.
    assert ThrottleCooldown(tmp_path / "cool").is_active() is True


@pytest.mark.asyncio
async def test_a_cooling_client_makes_no_requests(tmp_path):
    path = tmp_path / "cool"
    ThrottleCooldown(path).start(600)

    calls = []

    def handler(request):
        calls.append(request)
        return resp(200, json=OK_BODY)

    settings = make_settings(throttle_cooldown_path=str(path))
    c = make_client(handler, settings)
    raw = await c.post_graphql({})
    await c.close()

    assert failure_reason(raw) == "cooling_down"
    assert c.is_throttled is True
    assert calls == []  # the point: nothing reached the network


@pytest.mark.asyncio
async def test_expired_cooldown_lets_the_scan_resume(tmp_path):
    path = tmp_path / "cool"
    path.write_text((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())

    settings = make_settings(throttle_cooldown_path=str(path))
    c = make_client([resp(200, json=OK_BODY)], settings)
    raw = await c.post_graphql({})
    await c.close()

    assert failure_reason(raw) is None


@pytest.mark.asyncio
async def test_retry_after_beyond_ceiling_also_starts_a_cooldown(tmp_path, monkeypatch):
    async def fake_sleep(_):
        return None

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    settings = make_settings(
        throttle_cooldown_path=str(tmp_path / "cool"),
        max_retry_after_seconds=60.0,
    )
    c = make_client([resp(429, headers={"Retry-After": "600"})], settings)
    await c.post_graphql({})
    await c.close()

    cd = ThrottleCooldown(tmp_path / "cool")
    assert cd.is_active() is True
    assert cd.remaining_seconds() > 300  # the server's number, honoured
