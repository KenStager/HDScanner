"""Tests for setup-time store lookup.

The gateway distinguishes three benign-ish conditions from real failures, and
collapsing them is the bug this module exists to avoid: "no store within the
radius" must not read as "lookup broken", and a throttle must not read as
"no such store". Each is pinned here against the exact wording observed from
the live API.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from hd.hd_api import stores as st
from hd.hd_api.stores import (
    InvalidZipCode,
    StoreLookupError,
    StoreLookupThrottled,
    StoreResult,
    _classify,
    _to_result,
    get_store,
    search_stores,
)

HADLEY = {
    "storeId": "8452",
    "storeName": "Hadley",
    "distance": "1.1203389110006656",
    "phone": "(413) 587-4200",
    "address": {"street": "" , "city": "Hadley", "state": "MA", "postalCode": "01035"},
}
CHICOPEE = {
    "storeId": "2610",
    "storeName": "Chicopee",
    "distance": "12.22826997623999",
    "address": {"city": "Chicopee", "state": "MA", "postalCode": "01020"},
}


def _fake_post(data=None, errors=None):
    async def _inner(operation, query, variables, endpoint):
        return data or {}, errors or []
    return _inner


class TestToResult:
    def test_maps_every_store_field(self):
        r = _to_result(HADLEY)
        assert (r.store_id, r.name, r.city, r.state, r.zip) == (
            "8452", "Hadley", "Hadley", "MA", "01035"
        )
        assert r.distance_miles == pytest.approx(1.1203389, rel=1e-6)

    def test_missing_store_id_is_dropped(self):
        assert _to_result({"storeName": "Nowhere"}) is None
        assert _to_result({"storeId": "   "}) is None

    def test_missing_address_does_not_raise(self):
        r = _to_result({"storeId": "1", "storeName": "X"})
        assert r.city is None and r.is_complete is False

    def test_unparseable_distance_becomes_none(self):
        r = _to_result({"storeId": "1", "distance": "far away"})
        assert r.distance_miles is None


class TestStoreResult:
    def test_is_complete_requires_link_fields(self):
        assert _to_result(HADLEY).is_complete is True
        partial = StoreResult(store_id="8452", name="Hadley", city=None, state="MA", zip="01035")
        assert partial.is_complete is False

    def test_label_includes_distance_when_known(self):
        assert "8452" in _to_result(HADLEY).label
        assert "1.1 mi" in _to_result(HADLEY).label

    def test_label_omits_distance_when_unknown(self):
        assert "mi)" not in StoreResult("1", "X", "C", "MA", "01035").label


class TestClassify:
    """Wording taken verbatim from live gateway responses."""

    def test_no_records(self):
        assert _classify([{"message": "Store Search records not found"}]) == "no_records"

    def test_invalid_zip(self):
        assert _classify([{"message": "Invalid value for zipCode: "}]) == "invalid_zip"

    def test_invalid_store(self):
        assert _classify([{"message": "Please enter a valid store ID"}]) == "invalid_store"

    def test_unknown_error_returns_its_message(self):
        assert _classify([{"message": "Kaboom"}]) == "Kaboom"

    def test_empty_error_array(self):
        assert _classify([]) == "unknown error"


class TestSearchStores:
    async def test_returns_stores_nearest_first(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post({"storeSearch": [CHICOPEE, HADLEY]}))
        result = await search_stores("01035")
        assert [s.store_id for s in result] == ["8452", "2610"]

    async def test_no_records_is_an_empty_list_not_an_error(self, monkeypatch):
        """The whole point: 'widen your radius', not 'lookup failed'."""
        monkeypatch.setattr(
            st, "_post", _fake_post(errors=[{"message": "Store Search records not found"}])
        )
        assert await search_stores("59645", radius_miles=50.0) == []

    async def test_invalid_zip_raises_input_error(self, monkeypatch):
        monkeypatch.setattr(
            st, "_post", _fake_post(errors=[{"message": "Invalid value for zipCode: "}])
        )
        with pytest.raises(InvalidZipCode):
            await search_stores("....")

    async def test_empty_zip_rejected_without_a_request(self, monkeypatch):
        async def _boom(*a, **k):
            raise AssertionError("should not call the API for an empty ZIP")
        monkeypatch.setattr(st, "_post", _boom)
        with pytest.raises(InvalidZipCode):
            await search_stores("   ")

    async def test_unknown_error_surfaces_as_failure(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post(errors=[{"message": "Kaboom"}]))
        with pytest.raises(StoreLookupError):
            await search_stores("01035")

    async def test_malformed_rows_are_skipped(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post({"storeSearch": [{"noId": 1}, HADLEY]}))
        assert [s.store_id for s in await search_stores("01035")] == ["8452"]


class TestGetStore:
    async def test_returns_the_store(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post({"storeDetails": HADLEY}))
        assert (await get_store("8452")).city == "Hadley"

    async def test_unknown_store_id_is_none(self, monkeypatch):
        """Guards the transposed-digit case (8425 vs the real 8452)."""
        monkeypatch.setattr(
            st, "_post", _fake_post(errors=[{"message": "Please enter a valid store ID"}])
        )
        assert await get_store("8425") is None

    async def test_null_payload_is_none(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post({"storeDetails": None}))
        assert await get_store("99999") is None

    async def test_blank_id_rejected(self):
        with pytest.raises(StoreLookupError):
            await get_store("")


class TestTransport:
    """_post turns HTTP-level conditions into the right exception type."""

    @staticmethod
    def _run(stdout: str, returncode: int = 0):
        def _fake(*args, **kwargs):
            return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        return _fake

    async def test_206_is_throttling_not_a_generic_error(self, monkeypatch):
        """Home Depot signals rate limiting with 206, not 429."""
        monkeypatch.setattr(
            st.subprocess, "run", self._run('{"data":{"storeSearch":null}}\n206')
        )
        with pytest.raises(StoreLookupThrottled):
            await search_stores("01035")

    async def test_other_status_is_a_lookup_error(self, monkeypatch):
        monkeypatch.setattr(st.subprocess, "run", self._run("nope\n403"))
        with pytest.raises(StoreLookupError) as exc:
            await search_stores("01035")
        assert not isinstance(exc.value, StoreLookupThrottled)

    async def test_non_json_body(self, monkeypatch):
        monkeypatch.setattr(st.subprocess, "run", self._run("<html>blocked</html>\n200"))
        with pytest.raises(StoreLookupError):
            await search_stores("01035")

    async def test_curl_failure(self, monkeypatch):
        monkeypatch.setattr(st.subprocess, "run", self._run("", returncode=6))
        with pytest.raises(StoreLookupError):
            await search_stores("01035")

    async def test_timeout(self, monkeypatch):
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="curl", timeout=30)
        monkeypatch.setattr(st.subprocess, "run", _boom)
        with pytest.raises(StoreLookupError):
            await search_stores("01035")


class TestErrorPrecedence:
    """A benign marker must not mask a real failure.

    The gateway can return both. Reporting the benign one turns a genuine
    error into "no stores in range", so setup widens the radius against a
    failure and eventually tells the user no Home Depot exists near them.
    """

    def test_real_error_alongside_a_benign_one_wins(self):
        assert _classify([
            {"message": "Kaboom"},
            {"message": "Store Search records not found"},
        ]) == "Kaboom"

    def test_first_message_is_reported_not_the_last(self):
        assert _classify([{"message": "first"}, {"message": "second"}]) == "first"

    def test_input_error_preferred_over_no_records(self):
        assert _classify([
            {"message": "Store Search records not found"},
            {"message": "Invalid value for zipCode: "},
        ]) == "invalid_zip"

    async def test_masked_error_does_not_read_as_empty(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post(errors=[
            {"message": "Kaboom"},
            {"message": "Store Search records not found"},
        ]))
        with pytest.raises(StoreLookupError):
            await search_stores("01035")


class TestUnexplainedResponses:
    """A null payload with no errors is not "nothing found".

    Both genuine not-found conditions arrive as GraphQL errors, and a null
    payload is exactly the shape of a 206 throttle body — so treating it as an
    empty result would send a user with a valid ZIP away from setup.
    """

    async def test_null_store_search_raises(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post({"storeSearch": None}))
        with pytest.raises(StoreLookupError):
            await search_stores("01035")

    async def test_empty_list_is_still_a_legitimate_no_result(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post({"storeSearch": []}))
        assert await search_stores("01035") == []

    async def test_absent_store_details_raises(self, monkeypatch):
        monkeypatch.setattr(st, "_post", _fake_post({}))
        with pytest.raises(StoreLookupError):
            await get_store("8452")


class TestTransportEdges:
    async def test_missing_curl_is_reported_not_raised_raw(self, monkeypatch):
        """curl absent is a real first-run condition on minimal images."""
        def _boom(*a, **k):
            raise FileNotFoundError(2, "No such file or directory", "curl")

        monkeypatch.setattr(st.subprocess, "run", _boom)
        with pytest.raises(StoreLookupError) as exc:
            await search_stores("01035")
        assert "curl" in str(exc.value)

    async def test_206_with_an_empty_body_is_still_throttling(self, monkeypatch):
        """The documented throttle shape carries no body at all.

        Pins the status check ahead of JSON parsing: a body-carrying fixture
        alone would stay green if the order were reversed.
        """
        def _run(*a, **k):
            return types.SimpleNamespace(returncode=0, stdout="\n206", stderr="")

        monkeypatch.setattr(st.subprocess, "run", _run)
        with pytest.raises(StoreLookupThrottled):
            await search_stores("01035")

    async def test_zip_travels_in_the_body_never_the_url(self, monkeypatch):
        """Pins the injection-safe construction: variables, not interpolation."""
        captured = {}

        def _run(cmd, *a, **k):
            captured["cmd"] = cmd
            return types.SimpleNamespace(
                returncode=0, stdout='{"data":{"storeSearch":[]}}\n200', stderr=""
            )

        monkeypatch.setattr(st.subprocess, "run", _run)
        await search_stores("01035")
        cmd = captured["cmd"]
        url = cmd[cmd.index("--url") + 1]
        assert "01035" not in url
        payload = cmd[cmd.index("-d") + 1]
        assert '"zipCode": "01035"' in payload
        assert "shell" not in cmd
