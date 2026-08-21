"""Real-subprocess tests for CurlTransport against a local server.

The API refuses Python HTTP clients, so curl is the only transport the scanner
can use — which makes it worth proving that it really parses a status line,
response headers and a body, rather than trusting the happy path.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hd.http.transport import CurlTransport, TransportError, parse_header_block


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append({"headers": dict(self.headers), "payload": payload})

        status, extra, body = self.server.script
        raw = body.encode()
        self.send_response(status)
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    s.seen = []
    s.script = (200, {}, '{"ok": true}')
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def url_for(server):
    host, port = server.server_address
    return f"http://{host}:{port}/graphql"


@pytest.mark.asyncio
async def test_status_body_and_request_are_carried_through(server):
    t = CurlTransport()
    r = await t.post_json(url_for(server), {"a": 1}, {"User-Agent": "HDTest/1"})
    await t.close()

    assert r.status == 200
    assert json.loads(r.body) == {"ok": True}
    assert server.seen[0]["payload"] == {"a": 1}
    assert server.seen[0]["headers"]["User-Agent"] == "HDTest/1"


@pytest.mark.asyncio
async def test_response_headers_are_captured(server):
    """The previous implementation read only the status code, so Retry-After was invisible."""
    server.script = (429, {"Retry-After": "42"}, "slow down")
    t = CurlTransport()
    r = await t.post_json(url_for(server), {}, {})
    await t.close()

    assert r.status == 429
    assert r.header("Retry-After") == "42"
    assert r.header("retry-after") == "42"  # lookup is case-insensitive


@pytest.mark.asyncio
async def test_content_type_is_visible_for_challenge_detection(server):
    server.script = (200, {"Content-Type": "text/html"}, "<html>denied</html>")
    t = CurlTransport()
    r = await t.post_json(url_for(server), {}, {})
    await t.close()
    assert r.header("Content-Type").startswith("text/html")


@pytest.mark.asyncio
async def test_unreachable_host_raises_rather_than_returning_empty():
    t = CurlTransport(timeout_seconds=5)
    with pytest.raises(TransportError):
        # Port 1 on loopback refuses immediately.
        await t.post_json("http://127.0.0.1:1/graphql", {}, {})
    await t.close()


def test_header_block_parsing_ignores_status_lines():
    block = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nRetry-After: 9\r\n\r\n"
    assert parse_header_block(block) == {
        "Content-Type": "application/json",
        "Retry-After": "9",
    }


def test_later_header_block_wins_so_redirects_do_not_mask_the_final_response():
    block = "HTTP/1.1 301 Moved\r\nLocation: /x\r\n\r\nHTTP/1.1 200 OK\r\nLocation: /final\r\n\r\n"
    assert parse_header_block(block)["Location"] == "/final"
