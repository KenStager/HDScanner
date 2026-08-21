"""Tests for the pooled httpx client: identity, pooling, and retry policy."""

from __future__ import annotations

import asyncio
import json

import json as _json

import pytest

from hd.config import Settings
from hd.http import client as client_mod
from hd.http.transport import RawResponse, TransportError
from hd.http.client import (
    HDClient,
    backoff_seconds,
    build_headers,
    build_user_agent,
    parse_retry_after,
)
from hd.hd_api.graphql import failure_reason, is_valid_search_response


OK_BODY = {"data": {"searchModel": {"products": [], "searchReport": {"totalProducts": 0}}}}


def make_settings(**overrides):
    base = dict(
        _env_file=None,
        rate_limit_rps=1000.0,
        max_concurrency=2,
        jitter_min_ms=0,
        jitter_max_ms=0,
        request_budget=0,
        api_endpoint="https://api.example.invalid/graphql",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Run every test in a scratch directory.

    The client writes a throttle cooldown file relative to the working
    directory. Without this, one test's cooldown lands in the repo and
    silences every test that follows it — which is exactly what happened.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def slept(monkeypatch):
    """Capture sleep durations instead of waiting them out."""
    durations: list[float] = []

    async def fake_sleep(seconds):
        durations.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return durations


def resp(status, json=None, text=None, headers=None):
    """Build a transport response the way the API would return one."""
    body = _json.dumps(json) if json is not None else (text or "")
    hdrs = dict(headers or {})
    if json is not None:
        hdrs.setdefault("Content-Type", "application/json")
    return RawResponse(status=status, body=body, headers=hdrs)


class FakeTransport:
    """Scripted stand-in for CurlTransport. `responses` is a list or callable."""

    def __init__(self, responses):
        self.requests = []
        self.closed = False
        if callable(responses):
            self._handler = responses
        else:
            queue = list(responses)
            self._handler = lambda req: queue.pop(0) if queue else resp(200, json=OK_BODY)

    async def post_json(self, url, payload, headers):
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        return self._handler(self.requests[-1])

    async def close(self):
        self.closed = True


def make_client(responses, settings=None, **kwargs):
    """Client wired to a scripted transport."""
    settings = settings or make_settings()
    c = HDClient(settings, transport=FakeTransport(responses), **kwargs)
    c._query_cache = "query searchModel { x }"  # skip the on-disk lookup
    return c


# --- identity ---------------------------------------------------------------

def test_user_agent_names_the_scanner_not_a_browser():
    ua = build_user_agent(make_settings())
    assert "HDClearanceMonitor" in ua
    for browser in ("Mozilla", "Gecko", "Firefox", "Chrome", "Safari"):
        assert browser not in ua


def test_contact_email_is_appended_when_configured():
    ua = build_user_agent(make_settings(contact_email="ops@example.com"))
    assert ua == "HDClearanceMonitor/0.1 (+ops@example.com)"


def test_origin_is_dropped_but_referer_is_kept():
    headers = build_headers(make_settings())
    # Origin was tested alone against the live API and did not help: with an
    # honest User-Agent and no Referer the gateway answers 206 either way.
    assert "Origin" not in headers
    # Referer is what the gateway actually requires alongside an honest agent.
    assert headers["Referer"] == "https://www.homedepot.com/"
    # Gateway routing headers are API parameters and must survive.
    assert headers["x-experience-name"] == "general-merchandise"


@pytest.mark.asyncio
async def test_request_actually_sends_the_honest_user_agent():
    seen = {}

    def handler(request):
        seen.update(request["headers"])
        return resp(200, json=OK_BODY)

    c = make_client(handler, make_settings(contact_email="ops@example.com"))
    await c.post_graphql({})
    await c.close()

    assert seen["User-Agent"] == "HDClearanceMonitor/0.1 (+ops@example.com)"


# --- transport --------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_releases_the_transport():
    c = make_client([])
    await c.post_graphql({})
    await c.close()
    assert c._transport.closed is True


@pytest.mark.asyncio
async def test_every_request_carries_the_configured_headers():
    c = make_client([resp(200, json=OK_BODY)])
    await c.post_graphql({})
    await c.close()
    sent = c._transport.requests[0]["headers"]
    assert sent["User-Agent"].startswith("HDClearanceMonitor")
    # Required by the gateway alongside an honest User-Agent; see transport.py.
    assert sent["Referer"] == "https://www.homedepot.com/"


# --- Retry-After ------------------------------------------------------------

def test_parse_retry_after_seconds_form():
    assert parse_retry_after("120") == 120.0


def test_parse_retry_after_http_date_form():
    # A date already in the past means "go now", not a negative wait.
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_parse_retry_after_rejects_junk():
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("") is None


@pytest.mark.asyncio
async def test_429_waits_the_period_the_server_asked_for(slept):
    c = make_client([
        resp(429, headers={"Retry-After": "7"}),
        resp(200, json=OK_BODY),
    ])
    raw = await c.post_graphql({})
    await c.close()

    assert is_valid_search_response(raw) is True
    assert 7.0 in slept  # the server's number, not our backoff curve


@pytest.mark.asyncio
async def test_retry_after_beyond_the_ceiling_stops_the_run(slept):
    c = make_client(
        [resp(429, headers={"Retry-After": "3600"})],
        make_settings(max_retry_after_seconds=300.0),
    )
    raw = await c.post_graphql({})

    assert failure_reason(raw) == "http_429"
    assert c.is_throttled is True
    assert not any(s >= 3600 for s in slept)  # we stop, we do not wait it out

    # And a throttled client makes no further requests.
    again = await c.post_graphql({})
    assert failure_reason(again) == "throttled"
    assert c.request_count == 1
    await c.close()


@pytest.mark.asyncio
async def test_backoff_is_used_when_no_retry_after_given(slept):
    c = make_client([
        resp(503),
        resp(200, json=OK_BODY),
    ])
    raw = await c.post_graphql({})
    await c.close()

    assert is_valid_search_response(raw) is True
    # The limiter's own token-refill waits land here too, so identify the
    # backoff by its equal-jitter band rather than by being the only sleep.
    assert any(1.0 <= s <= 2.0 for s in slept)


def test_backoff_grows_and_stays_jittered():
    for attempt in (1, 2, 3, 4, 5):
        base = min(2.0 ** attempt, 60.0)
        samples = [backoff_seconds(attempt) for _ in range(50)]
        assert all(base / 2 <= s <= base for s in samples)
    # Jitter must actually vary, or concurrent retries resynchronise.
    assert len(set(backoff_seconds(4) for _ in range(50))) > 1


# --- failure accounting -----------------------------------------------------

@pytest.mark.asyncio
async def test_api_error_payload_is_counted_as_a_failure():
    c = make_client([resp(200, json={"errors": [{"message": "boom"}]})])
    raw = await c.post_graphql({})
    await c.close()

    # The raw payload still reaches the caller for inspection...
    assert raw["errors"][0]["message"] == "boom"
    # ...but it no longer slips past the tally, which made runs read cleaner
    # than they were.
    assert c.failures == {"api_error": 1}
    assert c.metrics.by_outcome()["api_error"] == 1


@pytest.mark.asyncio
async def test_oversize_response_is_refused(slept):
    big = {"data": {"searchModel": {"products": ["x" * 100]}}}
    c = make_client(
        [resp(200, json=big)],
        make_settings(max_response_bytes=50),
    )
    raw = await c.post_graphql({})
    await c.close()

    assert failure_reason(raw) == "oversize_response"


@pytest.mark.asyncio
async def test_transport_error_is_tagged_not_silently_empty(slept):
    def handler(request):
        raise TransportError("no route to host")

    c = make_client(handler, make_settings(max_attempts=2))
    raw = await c.post_graphql({})
    await c.close()

    assert failure_reason(raw) == "transport_error"
    assert is_valid_search_response(raw) is False
    assert c.metrics.by_outcome()["transport_error"] == 2


@pytest.mark.asyncio
async def test_403_stops_the_run_and_persists_a_cooldown(tmp_path):
    path = tmp_path / "cool"
    c = make_client(
        [resp(403)],
        make_settings(throttle_cooldown_path=str(path), forbidden_cooldown_seconds=600),
    )
    raw = await c.post_graphql({})

    assert failure_reason(raw) == "http_403"
    # Being refused ends the run rather than pausing and asking again.
    assert c.is_throttled is True
    again = await c.post_graphql({})
    assert failure_reason(again) == "throttled"
    assert c.request_count == 1
    await c.close()

    # And the next run inherits the refusal instead of rediscovering it.
    from hd.http.cooldown import ThrottleCooldown
    assert ThrottleCooldown(path).is_active() is True


@pytest.mark.asyncio
async def test_html_challenge_is_a_refusal_not_a_parse_failure(tmp_path):
    path = tmp_path / "cool"
    c = make_client(
        [resp(200, text="<html><body>Access Denied</body></html>")],
        make_settings(throttle_cooldown_path=str(path)),
    )
    raw = await c.post_graphql({})
    await c.close()

    # Previously this read as "bad_json" — the same label as a corrupt payload,
    # so being turned away looked like a glitch worth retrying.
    assert failure_reason(raw) == "challenge_html"
    assert c.is_throttled is True
    from hd.http.cooldown import ThrottleCooldown
    assert ThrottleCooldown(path).is_active() is True


@pytest.mark.asyncio
async def test_html_content_type_is_caught_even_without_a_leading_angle_bracket(tmp_path):
    c = make_client(
        [resp(200, text="  blocked  ", headers={"Content-Type": "text/html"})],
        make_settings(throttle_cooldown_path=str(tmp_path / "cool")),
    )
    raw = await c.post_graphql({})
    await c.close()
    assert failure_reason(raw) == "challenge_html"


@pytest.mark.asyncio
async def test_burst_capacity_is_one(tmp_path):
    """Every run used to open with three unpaced requests."""
    c = make_client([], make_settings(throttle_cooldown_path=str(tmp_path / "cool")))
    assert c._rate_limiter._burst == 1
    await c.close()


@pytest.mark.asyncio
async def test_206_quota_stops_the_run_immediately():
    c = make_client([resp(206, json=OK_BODY)])
    raw = await c.post_graphql({})
    await c.close()

    assert failure_reason(raw) == "http_206_quota"
    assert c.is_throttled is True


@pytest.mark.asyncio
async def test_bad_json_is_tagged():
    c = make_client([resp(200, text='{"data": {broken')])
    raw = await c.post_graphql({})
    await c.close()

    assert failure_reason(raw) == "bad_json"
    assert c.metrics.by_outcome()["bad_json"] == 1


@pytest.mark.asyncio
async def test_metrics_survive_a_full_run():
    c = make_client([
        resp(200, json=OK_BODY),
        resp(200, json=OK_BODY),
    ])
    await c.post_graphql({})
    await c.post_graphql({})
    await c.close()

    assert c.metrics.success_rate == 1.0
    assert c.metrics.attempts == 2
    assert c.metrics.by_status() == {"200": 2}
