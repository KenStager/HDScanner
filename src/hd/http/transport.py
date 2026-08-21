"""Request transport for the Home Depot GraphQL API.

Why curl and not a Python HTTP client: the gateway refuses Python's TLS/HTTP
fingerprint outright. Measured 2026-08-20, same payload, seconds apart:

    curl  + HDClearanceMonitor UA + Referer  -> 200 (24 products, 3/3)
    httpx + HDClearanceMonitor UA + Referer  -> 206
    httpx + Firefox UA + Referer + Origin    -> 206

httpx is turned away even wearing a complete browser header set, so this is not
about what the request claims — it is the client stack itself. That rules out
connection pooling: there is no pooled client the API will answer. The
subprocess-per-request cost is real and is the price of being answered at all.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol


class TransportError(Exception):
    """The request never produced an HTTP response."""


@dataclass(frozen=True)
class RawResponse:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class Transport(Protocol):
    async def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> RawResponse: ...

    async def close(self) -> None: ...


def parse_header_block(text: str) -> dict[str, str]:
    """Headers from curl's dump. Later blocks win, so redirects do not mask the final response."""
    headers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("HTTP/"):
            continue
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip()] = value.strip()
    return headers


class CurlTransport:
    """One curl process per request.

    Response headers are dumped to stderr and parsed, which the previous
    implementation did not do — it captured only the status code, so
    Retry-After was invisible and every wait was guesswork.
    """

    def __init__(self, timeout_seconds: float = 30.0, max_bytes: int = 10 * 1024 * 1024) -> None:
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes

    async def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> RawResponse:
        cmd = [
            "curl", "-s",
            "-D", "/dev/stderr",          # response headers, so Retry-After survives
            "-w", "\n%{http_code}",
            "-X", "POST",
            "--url", url,                 # --url so a leading "-" cannot become an option
            "--compressed",
            "--max-filesize", str(self._max_bytes),
            "--max-time", str(int(self._timeout)),
            "-d", json.dumps(payload),
        ]
        for name, value in headers.items():
            cmd.extend(["-H", f"{name}: {value}"])

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout + 5,
            )
        except FileNotFoundError as exc:
            raise TransportError("curl not found on PATH") from exc
        except OSError as exc:
            raise TransportError(f"could not run curl: {exc}") from exc

        if result.returncode != 0:
            raise TransportError(f"curl exited {result.returncode}")

        body, _, status_text = result.stdout.rpartition("\n")
        try:
            status = int(status_text.strip())
        except ValueError:
            raise TransportError("curl returned no status code") from None

        return RawResponse(
            status=status, body=body, headers=parse_header_block(result.stderr)
        )

    async def close(self) -> None:
        """Nothing to release: each request is its own process."""
        return None
