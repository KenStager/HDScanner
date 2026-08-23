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
    read_brand_facet,
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

    def __init__(self, throttled: bool = False, throttle_after: int | None = None):
        self.is_throttled = throttled
        self._throttle_after = throttle_after
        self.reads = 0

    def note_read(self) -> None:
        """Let a fake flip the client to throttled mid-sequence, as the real
        latching client does, rather than only ever being throttled up front."""
        self.reads += 1
        if self._throttle_after is not None and self.reads >= self._throttle_after:
            self.is_throttled = True


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Retry pauses are real in production and pointless in tests."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(br.asyncio, "sleep", _instant)


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:", store_raw_json=False)


def _search(*totals):
    """Fake hd.pipeline.brands.search. An int yields a valid response with that
    total; None yields a tagged failure, which is what a failed read looks like."""
    from hd.http.client import failure_response

    calls = {"n": 0}

    async def _inner(client, **kwargs):
        i = min(calls["n"], len(totals) - 1)
        calls["n"] += 1
        note = getattr(client, "note_read", None)
        if note:
            note()
        total = totals[i]
        if total is None:
            return failure_response("api_error")
        return {"data": {"searchModel": {"searchReport": {"totalProducts": total}}}}

    _inner.calls = calls
    return _inner


def _facets(*responses):
    """Expose (total, dimensions) fixtures through fetch_facets' real contract."""
    calls = {"n": 0}

    async def _inner(client, settings, nav_param, store_id, storefilter):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        note = getattr(client, "note_read", None)
        if note:
            note()
        total, dimensions = responses[i]
        return total, dimensions, None

    _inner.calls = calls
    return _inner


class TestListBrands:
    async def test_real_fetch_facets_contract(self, settings):
        """Brand reads consume the production helper's three-value result."""
        raw = {
            "data": {
                "searchModel": {
                    "searchReport": {"totalProducts": 34707},
                    "dimensions": [{
                        "label": "Brand",
                        "refinements": [{
                            "label": "MILWAUKEE",
                            "refinementKey": "zv",
                            "recordCount": 9306,
                        }],
                    }],
                }
            }
        }

        class FacetClient(FakeClient):
            async def post_graphql(self, variables):
                assert variables["navParam"] == settings.tools_nav_param
                assert variables["storeId"] == "8452"
                return raw

        got = await read_brand_facet(FacetClient(), settings, "8452")
        assert got == {"MILWAUKEE": ("zv", 9306)}

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
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        monkeypatch.setattr(br, "search", _search(2419))
        m = await resolve_brand(FakeClient(), settings, "ryobi", "8452")
        assert m.name == "RYOBI"
        assert m.token == "m5d"
        assert m.verified_total == 2419
        assert m.config_entry == "RYOBI:m5d"

    async def test_degraded_read_is_retried_before_declaring_absence(
        self, monkeypatch, settings
    ):
        """The core guard: a short response must not be believed."""
        fake = _facets((1356, DEGRADED), (34707, FULL))
        monkeypatch.setattr(br, "fetch_facets", fake)
        monkeypatch.setattr(br, "search", _search(9306))
        m = await resolve_brand(FakeClient(), settings, "Milwaukee", "8452")
        assert m is not None and m.token == "zv"

    async def test_brand_found_only_in_a_narrower_later_read(self, monkeypatch, settings):
        """The retry must consult each read, not the widest seen so far.

        Keeping only the widest map made retries inert whenever the first read
        happened to be the widest — the common case — and discarded a brand
        that appeared in a smaller later response.
        """
        wide_without = {"Brand": [
            {"label": "DEWALT", "token": "4j2", "count": 1},
            {"label": "RYOBI", "token": "m5d", "count": 1},
            {"label": "HUSKY", "token": "rd", "count": 1},
            {"label": "BOSCH", "token": "9u", "count": 1},
        ]}
        narrow_with = {"Brand": [
            {"label": "MILWAUKEE", "token": "zv", "count": 9306},
            {"label": "DEWALT", "token": "4j2", "count": 1},
        ]}
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, wide_without), (1356, narrow_with)))
        monkeypatch.setattr(br, "search", _search(9306))
        m = await resolve_brand(FakeClient(), settings, "Milwaukee", "8452")
        assert m is not None and m.token == "zv"

    async def test_genuinely_absent_brand_returns_none(self, monkeypatch, settings):
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        assert await resolve_brand(FakeClient(), settings, "Hilti", "8452", attempts=2) is None

    async def test_token_that_returns_no_products_is_rejected(self, monkeypatch, settings):
        """A token in the facet but genuinely empty must not reach the config."""
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        monkeypatch.setattr(br, "search", _search(0))
        assert await resolve_brand(FakeClient(), settings, "RYOBI", "8452") is None

    async def test_failed_verification_is_not_reported_as_absence(self, monkeypatch, settings):
        """A verify read that fails means "could not check", not "no such brand".

        Reporting it as absence would tell a user with a valid brand that it
        does not exist, and leave them with a config that scans nothing.
        """
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        monkeypatch.setattr(br, "search", _search(None))
        with pytest.raises(BrandResolutionError):
            await resolve_brand(FakeClient(), settings, "RYOBI", "8452", attempts=2)

    async def test_short_read_is_not_grounds_for_declaring_absence(self, monkeypatch, settings):
        """One implausibly small response must not settle the question."""
        tiny = {"Brand": [{"label": "WELLER", "token": "1lw", "count": 3}]}
        monkeypatch.setattr(br, "fetch_facets", _facets((10, tiny), (None, {})))
        with pytest.raises(BrandResolutionError):
            await resolve_brand(FakeClient(), settings, "MILWAUKEE", "8452", attempts=2)

    async def test_throttling_partway_through_is_reported_as_throttling(
        self, monkeypatch, settings
    ):
        """The real client latches throttled mid-sequence."""
        monkeypatch.setattr(br, "fetch_facets", _facets((34707, FULL)))
        monkeypatch.setattr(br, "search", _search(2419))
        client = FakeClient(throttle_after=2)
        with pytest.raises(BrandThrottled):
            await resolve_brand(client, settings, "RYOBI", "8452")

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
        monkeypatch.setattr(br, "search", _search(2419))
        assert await verify_token(FakeClient(), settings, "m5d", "8452") == 2419

    async def test_token_with_no_products_returns_none(self, monkeypatch, settings):
        monkeypatch.setattr(br, "search", _search(0))
        assert await verify_token(FakeClient(), settings, "zzzz9", "8452") is None

    async def test_failed_read_raises_rather_than_returning_none(self, monkeypatch, settings):
        monkeypatch.setattr(br, "search", _search(None))
        with pytest.raises(BrandResolutionError):
            await verify_token(FakeClient(), settings, "m5d", "8452")

    async def test_throttled_client_raises(self, monkeypatch, settings):
        monkeypatch.setattr(br, "search", _search(2419))
        with pytest.raises(BrandThrottled):
            await verify_token(FakeClient(throttled=True), settings, "m5d", "8452")

    async def test_token_containing_the_nav_separator_is_rejected(self, settings):
        """navParams join on "Z"; such a token would walk a different path."""
        with pytest.raises(BrandResolutionError):
            await verify_token(FakeClient(), settings, "aZb", "8452")


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
