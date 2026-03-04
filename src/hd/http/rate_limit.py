"""Async token bucket rate limiter with jitter."""

from __future__ import annotations

import asyncio
import random
import time


class TokenBucketRateLimiter:
    """Token bucket rate limiter with random jitter between requests."""

    def __init__(
        self,
        rps: float = 1.0,
        burst: int = 3,
        jitter_min_ms: int = 200,
        jitter_max_ms: int = 800,
    ) -> None:
        self._rate = rps
        self._burst = burst
        self._jitter_min_ms = jitter_min_ms
        self._jitter_max_ms = jitter_max_ms
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then apply random jitter."""
        async with self._lock:
            await self._wait_for_token()

        # Apply jitter after acquiring the token
        jitter_s = random.randint(self._jitter_min_ms, self._jitter_max_ms) / 1000.0
        await asyncio.sleep(jitter_s)

    async def _wait_for_token(self) -> None:
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Calculate wait time until next token
            wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now
