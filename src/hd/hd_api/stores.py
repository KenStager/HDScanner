"""Store lookup against the Home Depot federation gateway.

Setup-time only. The scan pipeline reaches the gateway through HDClient, which
is pinned to the searchModel operation and governed by a per-run request
budget. Store lookup is a different shape of call — a handful during
`hd setup`, never inside a scan — so it issues its own request rather than
borrowing the scan client's budget and circuit breaker.

Both operations were confirmed against the live gateway, which has schema
introspection enabled; the argument nullability below is what the server's own
validator requires. `storeSearch` returns StoreDetails objects directly, with
no `stores` wrapper field.

The gateway reports three distinct conditions that must not be collapsed into
one "lookup failed", because the right response to each differs:

  "Store Search records not found"  -> no store in range; widen the radius
  "Invalid value for zipCode"       -> bad ZIP; re-prompt
  "Please enter a valid store ID"   -> no such store; treat as not found
  HTTP 206 with a null body         -> throttling; wait and retry

Home Depot signals throttling with 206 rather than 429 — the same behaviour
http.client handles by setting _throttled and abandoning the run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from hd.http.client import CURL_HEADERS
from hd.logging import get_logger

log = get_logger("hd_api.stores")

DEFAULT_ENDPOINT = "https://apionline.homedepot.com/federation-gateway/graphql"

# Substrings the gateway uses for the two benign conditions. Matched loosely
# because the wording is theirs to change and a wording drift should degrade
# to a generic error, not a crash.
_NO_RECORDS_MARKER = "records not found"
_INVALID_ZIP_MARKER = "invalid value for zipcode"
_INVALID_STORE_MARKER = "valid store id"

# `address` carries the city that store page URLs need. Store.city is recorded
# rather than derived because it does not always match the store name.
_STORE_FIELDS = """
    storeId
    storeName
    distance
    phone
    address { street city state postalCode }
"""

STORE_SEARCH_QUERY = (
    "query storeSearch($zipCode: String!, $radius: Float!, $limit: Int) {"
    " storeSearch(zipCode: $zipCode, radius: $radius, limit: $limit) {"
    f"{_STORE_FIELDS}"
    " } }"
)

# storeId is declared non-null even though the schema accepts a nullable
# String: a non-null variable is always assignable to a nullable argument, and
# it makes an empty id fail at validation rather than return a null store.
STORE_DETAILS_QUERY = (
    "query storeDetails($storeId: String!) {"
    " storeDetails(storeId: $storeId) {"
    f"{_STORE_FIELDS}"
    " } }"
)


class StoreLookupError(RuntimeError):
    """The lookup could not be completed."""


class StoreLookupThrottled(StoreLookupError):
    """Home Depot is rate limiting us. Waiting and retrying is the fix."""


class InvalidZipCode(StoreLookupError):
    """The gateway rejected the ZIP code itself. Re-prompt the user."""


@dataclass(frozen=True)
class StoreResult:
    """One store as returned by the gateway.

    Field names mirror hd.db.models.Store so setup can seed a complete row. A
    store missing city/state/zip silently loses its store-page links, so these
    travel together rather than being filled in later.
    """

    store_id: str
    name: str | None
    city: str | None
    state: str | None
    zip: str | None
    distance_miles: float | None = None
    phone: str | None = None

    @property
    def is_complete(self) -> bool:
        """True when this store can build a working store-page URL."""
        return all((self.name, self.city, self.state, self.zip))

    @property
    def label(self) -> str:
        """Human-readable one-liner for the setup picker."""
        where = ", ".join(p for p in (self.city, self.state) if p)
        parts = [self.store_id, self.name or "(unnamed)"]
        if where:
            parts.append(where)
        if self.zip:
            parts.append(self.zip)
        line = "  ".join(parts)
        if self.distance_miles is not None:
            line += f"  ({self.distance_miles:.1f} mi)"
        return line


def _to_result(raw: dict[str, Any]) -> StoreResult | None:
    """Map one gateway StoreDetails object onto a StoreResult."""
    store_id = (raw.get("storeId") or "").strip()
    if not store_id:
        return None

    address = raw.get("address") or {}

    distance: float | None = None
    if raw.get("distance") is not None:
        # The gateway sends distance as a string ("1.1203389110006656").
        try:
            distance = float(raw["distance"])
        except (TypeError, ValueError):
            distance = None

    return StoreResult(
        store_id=store_id,
        name=(raw.get("storeName") or None),
        city=(address.get("city") or None),
        state=(address.get("state") or None),
        zip=(address.get("postalCode") or None),
        distance_miles=distance,
        phone=(raw.get("phone") or None),
    )


async def _post(
    operation: str, query: str, variables: dict[str, Any], endpoint: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """POST one GraphQL operation.

    Returns (data, errors). GraphQL-level errors are handed back rather than
    raised because their meaning is operation-specific — the caller decides
    which are benign. Transport, HTTP and throttle failures raise, since no
    caller can do anything useful with those.
    """
    payload = {"operationName": operation, "variables": variables, "query": query}

    cmd = [
        "curl", "-s", "-w", "\n%{http_code}",
        "-X", "POST",
        # --url, so an endpoint starting with "-" cannot be parsed as an option.
        "--url", f"{endpoint}?opname={operation}",
        "--compressed",
        "--max-filesize", "5242880",
        "-d", json.dumps(payload),
    ]
    for h in CURL_HEADERS:
        cmd.extend(["-H", h])

    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        raise StoreLookupError("Store lookup timed out after 30s") from exc
    except FileNotFoundError as exc:
        raise StoreLookupError(
            "curl is required for store lookup but was not found on PATH."
        ) from exc
    except OSError as exc:
        raise StoreLookupError(f"Could not run curl: {exc}") from exc

    if result.returncode != 0:
        raise StoreLookupError(f"Could not reach Home Depot (curl exit {result.returncode})")

    body, _, status_text = result.stdout.rpartition("\n")
    status = status_text.strip()

    # 206 is Home Depot's throttle signal, not a partial-content response.
    if status == "206":
        log.warning("Received 206 — throttled by Home Depot", operation=operation)
        raise StoreLookupThrottled(
            "Home Depot is rate limiting requests. Wait a minute and try again."
        )

    if status != "200":
        raise StoreLookupError(f"Home Depot returned HTTP {status or 'no status'}")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StoreLookupError("Home Depot returned a non-JSON response") from exc

    data = parsed.get("data") or {}
    errors = parsed.get("errors") or []
    return data, errors


def _classify(errors: list[dict[str, Any]]) -> str:
    """Reduce a GraphQL error array to a known kind, or its first message.

    Kinds: 'no_records' (nothing matched), 'invalid_zip' (bad ZIP),
    'invalid_store' (no such store id).

    A real error anywhere in the array wins over a benign marker. The gateway
    can return both, and returning the benign one would turn a genuine failure
    into "no stores in range" — which sends a user with a perfectly good ZIP
    away from setup, widening the radius against an error.
    """
    kinds: list[str] = []
    first_message = ""
    for err in errors:
        message = str(err.get("message") or "")
        if message and not first_message:
            first_message = message
        lowered = message.lower()
        if _NO_RECORDS_MARKER in lowered:
            kinds.append("no_records")
        elif _INVALID_ZIP_MARKER in lowered:
            kinds.append("invalid_zip")
        elif _INVALID_STORE_MARKER in lowered:
            kinds.append("invalid_store")
        else:
            # Unrecognised means unexplained; report it rather than a benign
            # sibling.
            return message or "unknown error"

    if not kinds:
        return first_message or "unknown error"
    # Input errors are more specific than "nothing found"; prefer them.
    for preferred in ("invalid_zip", "invalid_store", "no_records"):
        if preferred in kinds:
            return preferred
    return kinds[0]


async def search_stores(
    zip_code: str,
    *,
    radius_miles: float = 25.0,
    limit: int = 15,
    endpoint: str = DEFAULT_ENDPOINT,
) -> list[StoreResult]:
    """Find stores near a ZIP code, nearest first.

    Returns an empty list when the ZIP is usable but has no store within the
    radius — the caller should offer a wider search, not report a failure.
    Raises InvalidZipCode when the ZIP itself is rejected, and
    StoreLookupThrottled when Home Depot is rate limiting.
    """
    zip_code = zip_code.strip()
    if not zip_code:
        raise InvalidZipCode("A ZIP code is required")

    data, errors = await _post(
        "storeSearch",
        STORE_SEARCH_QUERY,
        {"zipCode": zip_code, "radius": float(radius_miles), "limit": limit},
        endpoint,
    )

    if errors:
        kind = _classify(errors)
        if kind == "no_records":
            log.info("No stores in range", zip=zip_code, radius=radius_miles)
            return []
        if kind == "invalid_zip":
            raise InvalidZipCode(f"{zip_code!r} is not a ZIP code Home Depot recognises")
        raise StoreLookupError(f"Store search failed: {kind}")

    raw_stores = data.get("storeSearch")
    if raw_stores is None:
        # Both genuine not-found conditions arrive as GraphQL errors, so a null
        # payload with none is unexplained — and it is exactly the shape a 206
        # throttle body has. Reporting it as "no stores near you" would send a
        # user with a good ZIP away.
        raise StoreLookupError("Home Depot returned an empty response for that search.")

    stores = [s for s in (_to_result(r) for r in raw_stores) if s]
    stores.sort(key=lambda s: (s.distance_miles is None, s.distance_miles or 0.0))

    log.info("Store search complete", zip=zip_code, radius=radius_miles, found=len(stores))
    return stores


async def get_store(store_id: str, *, endpoint: str = DEFAULT_ENDPOINT) -> StoreResult | None:
    """Look up one store by id. Returns None when no such store exists.

    Lets setup validate a hand-entered id instead of trusting it. An id that is
    merely wrong — transposed digits, say — resolves to nothing here rather
    than becoming a store row that is scanned forever and never returns data.
    """
    store_id = store_id.strip()
    if not store_id:
        raise StoreLookupError("A store id is required")

    data, errors = await _post(
        "storeDetails", STORE_DETAILS_QUERY, {"storeId": store_id}, endpoint
    )

    if errors:
        kind = _classify(errors)
        # Both "no records" and "not a valid store id" mean the same thing to a
        # caller validating a hand-typed id: there is no such store.
        if kind in ("no_records", "invalid_store"):
            log.info("No such store", store_id=store_id)
            return None
        raise StoreLookupError(f"Store lookup failed: {kind}")

    if "storeDetails" not in data:
        raise StoreLookupError("Home Depot returned an empty response for that store.")

    raw = data.get("storeDetails")
    return _to_result(raw) if raw else None
