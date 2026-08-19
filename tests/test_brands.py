"""Tests for brand-name to facet-token resolution.

The behaviour worth protecting is the retry: Home Depot's facet responses
degrade silently, so a brand missing from one read is not evidence the brand is
absent. Believing a single short read would tell a user their brand does not
exist and leave them with a config that scans nothing.
"""

from __future__ import annotations

import pytest

from hd.config import Settings
from hd.pipeline import brands as br
from hd.pipeline.brands import (
    BrandResolutionError,
    BrandThrottled,
    resolve_brand,
    list_brands,
    suggest_brands,
    verify_token,
)

FULL = {
    "Brand": [
        {"label": "MILWAUKEE", "token": "zv", "count": 9306},
        {"label": "DEWALT", "token": "4j2", "count": 2976},
        {"label": "RYOBI", "token": "m5d", "count": 2419},
    ]
}
DEGRADED = {"Brand": [{"label": "DEWALT", "token": "4j2", "count": 30}]}


class FakeClient:
    """Stands in for HDClient; only is_throttled is consulted."""

    def __init__(self, throttled: bool = False):
        self.is_throttled = throttled


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:", store_raw_json=False)


def _facets(*responses):
    """Queue of (total, dimensions) responses returned in order; last repeats."""
    calls = {"n": 0}

    async def _inner(client, settings, nav_param, store_id, storefilter):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    _inner.calls = calls
    return _inner


class TestListBrands:
    async def test_parses_labels_and_tokens(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        got = await list_brands(FakeClient(), settings, "8452")
        assert got["MILWAUKEE"] == ("zv", 9306)
        assert set(got) == {"MILWAUKEE", "DEWALT", "RYOBI"}

    async def test_retries_while_empty_then_succeeds(self, monkeypatch, settings):
        fake = _facets((None, {}), (None, {}), (34707, FULL))
        monkeypatch.setattr(br, "fetch_facets", fake)
        got = await list_brands(FakeClient(), settings, "8452")
        assert len(got) == 3
        assert fake.calls["n"] == 3

    async def test_gives_up_after_attempts(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((None, {})))
        with pytest.raises(BrandResolutionError):
            await list_brands(FakeClient(), settings, "8452", attempts=2)

    async def test_missing_brand_dimension_is_not_no_brands(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((10, {"Price": []})))
        with pytest.raises(BrandResolutionError):
            await list_brands(FakeClient(), settings, "8452", attempts=1)

    async def test_throttling_raises_rather_than_returning_empty(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((None, {})))
        with pytest.raises(BrandThrottled):
            await list_brands(FakeClient(throttled=True), settings, "8452")


class TestResolveBrand:
    async def test_resolves_and_verifies(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL), (2419, {})))
        m = await resolve_brand(FakeClient(), settings, "ryobi", "8452")
        assert m.name == "RYOBI"
        assert m.token == "m5d"
        assert m.verified_total == 2419
        assert m.config_entry == "RYOBI:m5d"

    async def test_degraded_read_is_retried_before_declaring_absence(
        self, monkeypatch, settings
    ):
        """The core guard: a short response must not be believed."""
        fake = _facets((1356, DEGRADED), (34707, FULL), (2419, {}))
        monkeypatch.setattr(br, "fetch_facets", fake)
        m = await resolve_brand(FakeClient(), settings, "Milwaukee", "8452")
        assert m is not None and m.token == "zv"

    async def test_genuinely_absent_brand_returns_none(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        assert await resolve_brand(FakeClient(), settings, "Hilti", "8452", attempts=2) is None

    async def test_token_that_returns_no_products_is_rejected(self, monkeypatch, settings):
        """A token in the facet but unusable must not reach the config."""
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL), (None, {})))
        assert await resolve_brand(FakeClient(), settings, "RYOBI", "8452") is None

    async def test_skipping_verification_keeps_the_match(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        m = await resolve_brand(FakeClient(), settings, "RYOBI", "8452", verify=False)
        assert m.verified_total is None and m.token == "m5d"

    async def test_blank_name_rejected(self, settings):
        with pytest.raises(BrandResolutionError):
            await resolve_brand(FakeClient(), settings, "  ", "8452")

    async def test_throttling_is_not_reported_as_absence(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((None, {})))
        with pytest.raises(BrandThrottled):
            await resolve_brand(FakeClient(throttled=True), settings, "RYOBI", "8452")


class TestVerifyToken:
    async def test_returns_total(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((2419, {})))
        assert await verify_token(FakeClient(), settings, "m5d", "8452") == 2419

    async def test_bogus_token_returns_none(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((None, {})))
        assert await verify_token(FakeClient(), settings, "zzzz9", "8452") is None


class TestSuggestBrands:
    AVAILABLE = {"MILWAUKEE": ("zv", 1), "DEWALT": ("4j2", 1), "RYOBI": ("m5d", 1)}

    @pytest.mark.parametrize("typo,expected", [
        ("milwauk", "MILWAUKEE"),
        ("rioby", "RYOBI"),
        ("dewalt", "DEWALT"),
    ])
    def test_finds_intended_brand(self, typo, expected):
        assert expected in suggest_brands(typo, self.AVAILABLE)

    def test_no_match_returns_empty(self):
        assert suggest_brands("zzzzzz", self.AVAILABLE) == []
