"""Tests that a failed request cannot masquerade as an empty result.

This is the defect that made every coverage number untrustworthy: throttling,
budget exhaustion, 403s and timeouts all returned the same empty-shaped payload
that the pipelines accepted as "no more products", so pagination stopped early
and the run reported success.
"""

from __future__ import annotations

import pytest

from hd.hd_api.graphql import failure_reason, is_valid_search_response
from hd.http.client import FAILURE_KEY, failure_response


REAL_EMPTY = {"data": {"searchModel": {"products": [], "searchReport": {"totalProducts": 0}}}}


def test_genuine_empty_result_is_valid():
    """A real "this category has nothing" answer must still be usable."""
    assert is_valid_search_response(REAL_EMPTY) is True
    assert failure_reason(REAL_EMPTY) is None


@pytest.mark.parametrize("reason", [
    "throttled", "budget_exhausted", "http_403", "http_206_quota",
    "http_429", "http_5xx", "empty_body", "timeout", "bad_json", "exception",
])
def test_every_failure_reason_is_rejected(reason):
    raw = failure_response(reason)
    assert is_valid_search_response(raw) is False
    assert failure_reason(raw) == reason


def test_failure_is_shaped_like_a_real_response():
    """It must stay shape-compatible; only the marker distinguishes it."""
    raw = failure_response("timeout")
    assert raw["data"]["searchModel"]["products"] == []
    assert FAILURE_KEY in raw


def test_failure_is_distinguishable_from_real_empty():
    assert is_valid_search_response(REAL_EMPTY) != is_valid_search_response(
        failure_response("throttled")
    )


def test_api_error_payload_still_rejected():
    assert is_valid_search_response({"errors": [{"message": "boom"}]}) is False


def test_missing_search_model_rejected():
    assert is_valid_search_response({"data": {}}) is False


def test_non_dict_rejected():
    assert is_valid_search_response(None) is False
    assert is_valid_search_response([]) is False
    assert failure_reason(None) == "not_a_dict"


class _StubSettings:
    """Minimal stand-in so the client can be built without real config."""
    rate_limit_rps = 1.0
    max_concurrency = 1
    jitter_min_ms = 0
    jitter_max_ms = 0
    circuit_breaker_failure_threshold = 10
    circuit_breaker_window_seconds = 60
    request_budget = 2
    api_endpoint = "https://example.invalid/graphql"


@pytest.mark.asyncio
async def test_budget_exhaustion_is_tagged_and_counted():
    from hd.http.client import HDClient

    client = HDClient(_StubSettings())
    client._request_count = 99  # past the stub budget of 2
    raw = await client.post_graphql({})

    assert failure_reason(raw) == "budget_exhausted"
    assert is_valid_search_response(raw) is False
    assert client.failures == {"budget_exhausted": 1}
    assert client.failure_count == 1


@pytest.mark.asyncio
async def test_throttled_client_short_circuits_without_requesting():
    from hd.http.client import HDClient

    client = HDClient(_StubSettings())
    client._throttled = True
    raw = await client.post_graphql({})

    assert failure_reason(raw) == "throttled"
    assert client.request_count == 0  # never reached the network
