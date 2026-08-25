"""HTTP client wrapper for Home Depot API requests."""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import time
from collections import deque
from dataclasses import replace
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hd.config import Settings
from hd.http.cooldown import ThrottleCooldown
from hd.http.metrics import RequestMetrics
from hd.http.transport import (
    CurlTransport,
    RawResponse,
    Transport,
    TransportError,
)
from hd.http.rate_limit import TokenBucketRateLimiter
from hd.logging import get_logger

log = get_logger("http.client")


def _observe_response(status: int, response: object, context: dict) -> None:
    """Hand an unusual response to an optional diagnostics sink, if one is present.

    A convention like the CLI's plugin seam: a stock clone has no such module and
    neither knows nor cares that one can exist. Never raises into the request path.
    """
    try:
        from hd.plugins.diagnostics import on_response
    except Exception:
        return
    try:
        on_response(status, response, context)
    except Exception:
        pass


# Headers the federation gateway needs to route the request. These are API
# parameters, not browser emulation: the gateway selects a schema from
# x-experience-name and a datacentre from x-hd-dc.
API_HEADERS = {
    # Ordinary request headers any HTTP client may send. Stripping these was an
    # over-correction: they are content negotiation, not disguise — the same
    # reasoning under which Accept-Language was kept in the daily-deals fetch.
    # Accept-Encoding is left to curl --compressed, which advertises only the
    # codecs it can actually decode.
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Content-Type": "application/json",
    # Required in practice: with an honest User-Agent and no Referer the
    # gateway answers 206, with it 200 (measured 3/3 both ways, 2026-08-20).
    # Origin does not substitute — tested alone, it still 206s. This header is
    # not accurate: nothing here came from a page load on homedepot.com. It is
    # kept because the User-Agent alongside it says exactly what this client is
    # and how to reach its operator, so the request as a whole conceals nothing.
    "Referer": "https://www.homedepot.com/",
    # Gateway parameters: schema selection, datacentre, and the debug flag.
    # None of these assert anything about what kind of client is calling.
    "x-experience-name": "general-merchandise",
    "x-hd-dc": "origin",
    "x-debug": "false",
}


def build_user_agent(settings: Settings) -> str:
    """Identify this scanner honestly, with a contact address when configured.

    A User-Agent that names the tool is what lets the operator on the other end
    allow-list it, rate-limit it deliberately, or ask it to stop. A borrowed
    browser string forecloses all three.
    """
    ua = getattr(settings, "user_agent", "") or "HDClearanceMonitor/0.1"
    contact = getattr(settings, "contact_email", "")
    return f"{ua} (+{contact})" if contact else ua


def build_headers(settings: Settings) -> dict[str, str]:
    headers = {**API_HEADERS, "User-Agent": build_user_agent(settings)}
    return _apply_header_override(headers, settings)


def _apply_header_override(
    headers: dict[str, str], settings: Settings
) -> dict[str, str]:
    """Apply an optional private header policy; honest defaults if none is present.

    Same convention as the CLI's plugin seam: a stock clone has no such module,
    so it sends exactly the honest headers above. Never raises into the request.
    """
    try:
        from hd.plugins.headers import override_headers
    except Exception:
        return headers
    try:
        return override_headers(headers, settings)
    except Exception:
        return headers


def header_lines(settings: Settings) -> list[str]:
    """The same headers as "Name: value" strings, for callers that shell out.

    Store lookup still uses curl: it runs twice during setup, so pooling buys
    it nothing, but it must identify itself the same way the scanner does.
    """
    return [f"{k}: {v}" for k, v in build_headers(settings).items()]


# Marker key stamped onto synthetic empty responses so downstream code can tell
# "the API legitimately returned no products" apart from "we never got an answer".
# Without this every failure looks like end-of-results and pagination stops early,
# silently reporting partial coverage as complete.
FAILURE_KEY = "_hd_failure"


def _outcome_for(status_code: int) -> str:
    """Metrics label for an HTTP status. Refined later for body-level failures."""
    if status_code == 403:
        return "http_403"
    if status_code == 206:
        return "http_206_quota"
    if status_code == 429:
        return "http_429"
    if status_code >= 500:
        return "http_5xx"
    if status_code == 0:
        return "no_status"
    return "ok"


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait per a Retry-After header, or None if absent/unparseable.

    Accepts both forms in RFC 9110: delta-seconds and an HTTP-date. A date in
    the past yields 0.0 rather than a negative wait.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def backoff_seconds(attempt: int, cap: float = 60.0) -> float:
    """Exponential backoff with equal jitter.

    Jitter matters because the scanner fires concurrent requests: without it a
    burst of 429s retries in lockstep and rebuilds the same spike that caused
    the throttling.
    """
    base = min(2.0 ** attempt, cap)
    return base / 2 + random.uniform(0, base / 2)


def failure_response(reason: str) -> dict[str, Any]:
    """Empty-shaped response tagged with why it is empty."""
    return {"data": {"searchModel": {"products": []}}, FAILURE_KEY: reason}


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open."""
    pass


class CircuitBreaker:
    """Rolling window circuit breaker."""

    def __init__(self, threshold: int = 10, window_seconds: int = 60) -> None:
        self._threshold = threshold
        self._window = window_seconds
        self._failures: deque[float] = deque()
        self._is_open = False

    def check(self) -> None:
        """Raise CircuitOpenError if the circuit is open."""
        self._prune()
        if len(self._failures) < self._threshold:
            self._is_open = False
            return
        self._is_open = True
        raise CircuitOpenError(
            f"Circuit breaker open: {len(self._failures)} failures "
            f"in {self._window}s window (threshold={self._threshold})"
        )

    def record_failure(self) -> None:
        self._failures.append(time.monotonic())
        self._prune()
        if len(self._failures) >= self._threshold:
            self._is_open = True

    def record_success(self) -> None:
        self._prune()
        if self._is_open and len(self._failures) < self._threshold:
            self._is_open = False

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._window
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()


class HDClient:
    """Async client for the Home Depot GraphQL API.

    Sends through curl — see hd.http.transport for the measurements that rule
    out a pooled Python client. Identifies itself by name; see build_user_agent.
    """

    def __init__(
        self,
        settings: Settings,
        request_budget: int | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport or CurlTransport(
            timeout_seconds=getattr(settings, "read_timeout_seconds", 30.0),
            max_bytes=getattr(settings, "max_response_bytes", 10 * 1024 * 1024),
        )
        self._rate_limiter = TokenBucketRateLimiter(
            rps=settings.rate_limit_rps,
            burst=getattr(settings, "rate_limit_burst", 1),
            jitter_min_ms=settings.jitter_min_ms,
            jitter_max_ms=settings.jitter_max_ms,
        )
        self._circuit_breaker = CircuitBreaker(
            threshold=settings.circuit_breaker_failure_threshold,
            window_seconds=settings.circuit_breaker_window_seconds,
        )
        self._query_cache: str | None = None
        self._request_count: int = 0
        self._request_budget: int = (
            request_budget if request_budget is not None else settings.request_budget
        )
        self._throttled: bool = False
        self._failures: dict[str, int] = {}
        self._metrics = RequestMetrics()
        self._cooldown = ThrottleCooldown(
            getattr(settings, "throttle_cooldown_path", ".hd_throttle_cooldown"),
            getattr(settings, "throttle_cooldown_seconds", 3600.0),
        )

    def _count_failure(self, reason: str) -> None:
        self._failures[reason] = self._failures.get(reason, 0) + 1

    def _fail(self, reason: str) -> dict[str, Any]:
        """Record a failed request and return a tagged empty response."""
        self._count_failure(reason)
        return failure_response(reason)

    @property
    def metrics(self) -> RequestMetrics:
        """Latency and status-code metrics for this client's lifetime."""
        return self._metrics

    @property
    def failures(self) -> dict[str, int]:
        """Failure counts by reason for this client's lifetime."""
        return dict(self._failures)

    @property
    def failure_count(self) -> int:
        return sum(self._failures.values())

    def _load_query(self) -> str:
        if self._query_cache is None:
            current = Path(__file__).resolve().parent
            for _ in range(5):
                candidate = current / "queries" / "searchModel.graphql"
                if candidate.exists():
                    self._query_cache = candidate.read_text().strip()
                    return self._query_cache
                current = current.parent
            raise FileNotFoundError("Cannot find queries/searchModel.graphql")
        return self._query_cache

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def is_throttled(self) -> bool:
        return self._throttled

    @property
    def cooldown(self) -> ThrottleCooldown:
        return self._cooldown

    async def post_graphql(self, variables: dict[str, Any]) -> dict:
        """Send a GraphQL request with rate limiting, circuit breaker, and retry."""
        if self._throttled:
            return self._fail("throttled")
        if self._cooldown.is_active():
            # Checked per request rather than once at startup so a cooldown
            # written mid-run — by this client or a concurrent one — takes hold
            # immediately.
            log.warning(
                "In throttle cooldown — not requesting",
                resumes_in_seconds=round(self._cooldown.remaining_seconds()),
            )
            return self._fail("cooling_down")
        if self._request_budget > 0 and self._request_count >= self._request_budget:
            log.warning(
                "Request budget exhausted",
                count=self._request_count,
                budget=self._request_budget,
            )
            return self._fail("budget_exhausted")
        self._circuit_breaker.check()
        await self._rate_limiter.acquire()
        self._request_count += 1
        return await self._do_request(variables)

    async def _wait_or_stop(self, retry_after: float | None, attempt: int) -> bool:
        """Sleep before a retry. False means the server asked for longer than we honour.

        A Retry-After beyond the ceiling is not an obstacle to wait out — it is
        the API saying come back later, so the run stops and reports it.
        """
        ceiling = getattr(self._settings, "max_retry_after_seconds", 300.0)
        if retry_after is not None and retry_after > ceiling:
            self._throttled = True
            self._cooldown.start(retry_after)
            log.warning(
                "Retry-After exceeds ceiling — stopping this run",
                retry_after_seconds=retry_after,
                ceiling_seconds=ceiling,
            )
            return False
        delay = retry_after if retry_after is not None else backoff_seconds(attempt)
        await asyncio.sleep(delay)
        return True

    async def _do_request(self, variables: dict[str, Any], attempt: int = 1) -> dict:
        if attempt > 1:
            await self._rate_limiter.acquire()
        query = self._load_query()

        payload = {
            "operationName": "searchModel",
            "variables": variables,
            "query": query,
        }
        url = f"{self._settings.api_endpoint}?opname=searchModel"
        max_attempts = getattr(self._settings, "max_attempts", 5)
        max_bytes = getattr(self._settings, "max_response_bytes", 10 * 1024 * 1024)

        started = time.monotonic()
        try:
            response = await self._transport.post_json(
                url, payload, build_headers(self._settings)
            )
            elapsed_ms = (time.monotonic() - started) * 1000

            status_code = response.status
            retry_after = parse_retry_after(response.header("Retry-After"))
            self._metrics.record(
                _outcome_for(status_code), elapsed_ms, status_code, attempt
            )

            if len(response.body) > max_bytes:
                self._metrics.records[-1] = replace(
                    self._metrics.records[-1], outcome="oversize_response"
                )
                log.warning(
                    "Response exceeds size limit",
                    size=len(response.body),
                    limit=max_bytes,
                )
                self._circuit_breaker.record_failure()
                return self._fail("oversize_response")

            refusal_context = {
                "request_index": self._request_count,
                "attempt": attempt,
                "nav_param": variables.get("navParam"),
                "start_index": variables.get("startIndex"),
                "page_size": variables.get("pageSize"),
                "storefilter": variables.get("storefilter"),
            }

            if status_code == 403:
                _observe_response(status_code, response, refusal_context)
                # A refusal ends the run. Pausing 30s and moving to the next
                # walk meant answering "no" by asking again 51 more times;
                # the circuit breaker caught it at ten, but only after ten.
                self._throttled = True
                until = self._cooldown.start(
                    getattr(self._settings, "forbidden_cooldown_seconds", 3600.0)
                )
                log.warning(
                    "Received 403 — the API declined this request; stopping",
                    user_agent=build_user_agent(self._settings),
                    cooldown_until=until.isoformat(),
                )
                self._circuit_breaker.record_failure()
                return self._fail("http_403")

            if status_code == 206:
                _observe_response(status_code, response, refusal_context)
                self._throttled = True
                until = self._cooldown.start(retry_after)
                log.warning(
                    "Received 206 — quota exhausted, aborting further requests",
                    request_count=self._request_count,
                    cooldown_until=until.isoformat(),
                )
                return self._fail("http_206_quota")

            if status_code == 429:
                log.warning("Received 429 — rate limited", retry_after=retry_after)
                self._circuit_breaker.record_failure()
                if attempt < max_attempts:
                    if await self._wait_or_stop(retry_after, attempt):
                        return await self._do_request(variables, attempt + 1)
                return self._fail("http_429")

            if status_code >= 500:
                log.warning("Server error", status=status_code)
                self._circuit_breaker.record_failure()
                if attempt < max_attempts:
                    if await self._wait_or_stop(retry_after, attempt):
                        return await self._do_request(variables, attempt + 1)
                return self._fail("http_5xx")

            body = response.body

            # A block or challenge page is HTML with a 200. Left to json.loads
            # it became "bad_json", indistinguishable from a corrupt payload —
            # so being turned away looked like a glitch worth retrying.
            content_type = response.header("Content-Type") or ""
            if "html" in content_type.lower() or body.lstrip()[:1] == "<":
                _observe_response(status_code, response, refusal_context)
                self._metrics.records[-1] = replace(
                    self._metrics.records[-1], outcome="challenge_html"
                )
                self._throttled = True
                until = self._cooldown.start(
                    getattr(self._settings, "forbidden_cooldown_seconds", 3600.0)
                )
                log.warning(
                    "Received an HTML page where JSON was expected — treating as "
                    "a refusal and stopping",
                    status=status_code,
                    content_type=content_type or "(none)",
                    cooldown_until=until.isoformat(),
                )
                self._circuit_breaker.record_failure()
                return self._fail("challenge_html")

            if not body.strip():
                self._metrics.records[-1] = replace(
                    self._metrics.records[-1], outcome="empty_body"
                )
                log.warning("Empty response body", status=status_code)
                self._circuit_breaker.record_failure()
                return self._fail("empty_body")

            data = json.loads(body)

            # Check for API error responses (valid JSON but contains error payload)
            if isinstance(data, dict) and ("error" in data or "errors" in data):
                self._metrics.records[-1] = replace(
                    self._metrics.records[-1], outcome="api_error"
                )
                log.warning(
                    "API returned error response",
                    status=status_code,
                    error_keys=[k for k in ("error", "errors") if k in data],
                )
                self._circuit_breaker.record_failure()
                # Counted as well as returned: an error payload is a failed
                # request, and leaving it out of the tally made health checks
                # read cleaner than the run actually was.
                self._count_failure("api_error")
                # An origin-certain payload (graphql-java parsed our variables):
                # the diagnostics sink keeps these to settle the edge/origin
                # question. Guarded and no-op without the private overlay.
                _observe_response(
                    status_code, response, {**refusal_context, "outcome": "api_error"}
                )
                return data  # Return raw error for upstream inspection

            # Hand successful responses to the sink too, so it can bank a
            # header baseline (which headers a normal 200 even carries). The
            # sink keeps only the first per run; ordinary 200s are dropped
            # there, not here, so the request path is unchanged.
            _observe_response(
                status_code, response, {**refusal_context, "outcome": "ok"}
            )
            self._circuit_breaker.record_success()
            return data

        except subprocess.TimeoutExpired:
            self._metrics.record(
                "timeout", (time.monotonic() - started) * 1000, None, attempt
            )
            self._circuit_breaker.record_failure()
            log.warning("Request timed out")
            if attempt < max_attempts:
                if await self._wait_or_stop(None, attempt):
                    return await self._do_request(variables, attempt + 1)
            return self._fail("timeout")
        except TransportError as e:
            self._metrics.record(
                "transport_error", (time.monotonic() - started) * 1000, None, attempt
            )
            self._circuit_breaker.record_failure()
            log.warning("Transport error", error=str(e))
            if attempt < max_attempts:
                if await self._wait_or_stop(None, attempt):
                    return await self._do_request(variables, attempt + 1)
            return self._fail("transport_error")
        except json.JSONDecodeError as e:
            self._metrics.records[-1] = replace(
                self._metrics.records[-1], outcome="bad_json"
            )
            self._circuit_breaker.record_failure()
            log.error("Failed to parse response JSON", error=str(e))
            return self._fail("bad_json")
        except Exception as e:
            self._metrics.record(
                "exception", (time.monotonic() - started) * 1000, None, attempt
            )
            self._circuit_breaker.record_failure()
            log.error("Request failed", error=str(e))
            return self._fail("exception")

    async def close(self) -> None:
        """Release the transport."""
        await self._transport.close()
