"""HTTP client wrapper for Home Depot API requests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

from hd.config import Settings
from hd.http.rate_limit import TokenBucketRateLimiter
from hd.logging import get_logger

log = get_logger("http.client")

CURL_HEADERS = [
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Accept: */*",
    "Accept-Language: en-US,en;q=0.5",
    "Accept-Encoding: gzip, deflate, br, zstd",
    "Referer: https://www.homedepot.com/",
    "Content-Type: application/json",
    "Origin: https://www.homedepot.com",
    "x-experience-name: general-merchandise",
    "x-hd-dc: origin",
    "x-debug: false",
]


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
    """Async HTTP client for Home Depot GraphQL API.

    Uses curl subprocess for requests to maintain proper TLS fingerprinting
    that the HD API expects from browser-like clients.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._rate_limiter = TokenBucketRateLimiter(
            rps=settings.rate_limit_rps,
            burst=settings.max_concurrency,
            jitter_min_ms=settings.jitter_min_ms,
            jitter_max_ms=settings.jitter_max_ms,
        )
        self._circuit_breaker = CircuitBreaker(
            threshold=settings.circuit_breaker_failure_threshold,
            window_seconds=settings.circuit_breaker_window_seconds,
        )
        self._query_cache: str | None = None

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

    async def post_graphql(self, variables: dict[str, Any]) -> dict:
        """Send a GraphQL request with rate limiting, circuit breaker, and retry."""
        self._circuit_breaker.check()
        await self._rate_limiter.acquire()
        return await self._do_request(variables)

    async def _do_request(self, variables: dict[str, Any], attempt: int = 1) -> dict:
        if attempt > 1:
            await self._rate_limiter.acquire()
        query = self._load_query()

        payload = {
            "operationName": "searchModel",
            "variables": variables,
            "query": query,
        }

        cmd = [
            "curl", "-s", "-w", "\n%{http_code}",
            "-X", "POST",
            f"{self._settings.api_endpoint}?opname=searchModel",
            "--compressed",
            "--max-filesize", "10485760",
            "-d", json.dumps(payload),
        ]
        for h in CURL_HEADERS:
            cmd.extend(["-H", h])

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Parse status code from the last line
            lines = result.stdout.rsplit("\n", 1)
            body = lines[0] if len(lines) > 1 else result.stdout
            status_code = int(lines[1]) if len(lines) > 1 else 0

            if status_code == 403:
                log.warning("Received 403 — possible bot detection, backing off 30s")
                self._circuit_breaker.record_failure()
                await asyncio.sleep(30)
                return {"data": {"searchModel": {"products": []}}}

            if status_code == 429:
                log.warning("Received 429 — rate limited")
                self._circuit_breaker.record_failure()
                if attempt < 5:
                    await asyncio.sleep(min(2 ** attempt, 60))
                    return await self._do_request(variables, attempt + 1)
                return {"data": {"searchModel": {"products": []}}}

            if status_code >= 500:
                log.warning("Server error", status=status_code)
                self._circuit_breaker.record_failure()
                if attempt < 5:
                    await asyncio.sleep(min(2 ** attempt, 60))
                    return await self._do_request(variables, attempt + 1)
                return {"data": {"searchModel": {"products": []}}}

            if not body.strip():
                log.warning("Empty response body", status=status_code)
                self._circuit_breaker.record_failure()
                return {"data": {"searchModel": {"products": []}}}

            data = json.loads(body)

            # Check for API error responses (valid JSON but contains error payload)
            if isinstance(data, dict) and ("error" in data or "errors" in data):
                log.warning(
                    "API returned error response",
                    status=status_code,
                    error_keys=[k for k in ("error", "errors") if k in data],
                )
                self._circuit_breaker.record_failure()
                return data  # Return raw error for upstream inspection

            self._circuit_breaker.record_success()
            return data

        except subprocess.TimeoutExpired:
            self._circuit_breaker.record_failure()
            log.warning("Request timed out")
            if attempt < 5:
                await asyncio.sleep(min(2 ** attempt, 60))
                return await self._do_request(variables, attempt + 1)
            return {"data": {"searchModel": {"products": []}}}
        except json.JSONDecodeError as e:
            self._circuit_breaker.record_failure()
            log.error("Failed to parse response JSON", error=str(e))
            return {"data": {"searchModel": {"products": []}}}
        except Exception as e:
            self._circuit_breaker.record_failure()
            log.error("Request failed", error=str(e))
            return {"data": {"searchModel": {"products": []}}}

    async def close(self) -> None:
        """No persistent connection to close with curl backend."""
        pass
