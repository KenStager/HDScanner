"""Resolve a brand name to the facet token browse mode needs.

Browse mode walks `root_nav_param Z <token>`, where the token is an opaque
Home Depot facet id — Milwaukee is "zv", DEWALT is "4j2". There is no public
lookup for these, so before this module the only way to add a brand was to
find its token by hand. A brand configured without one is worse than an error:
`run_browse` walks only brands present in brand_tokens, so the run succeeds and
scans nothing.

The tokens are published in the catalog's own Brand facet. Reading them back
reproduces both hand-found tokens exactly, and the same tokens are returned at
stores in different states — brand facets are catalog-global, so resolution
happens once rather than per store.

Two properties of the API shape this module:

  * Responses degrade silently. The same facet read has returned 166 brands
    once and 225 moments later, with no error either time. A brand missing
    from one response is therefore not evidence the brand is absent, so a miss
    is retried before it is believed.
  * Throttling arrives as HTTP 206, which HDClient converts into a tagged
    failure and a latched _throttled flag. That is reported as throttling
    rather than as "brand not found", which would be a lie.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from hd.config import Settings
from hd.http.client import HDClient
from hd.logging import get_logger
from hd.pipeline.browse import build_nav, fetch_facets

log = get_logger("pipeline.brands")

BRAND_DIMENSION = "Brand"
DEFAULT_ATTEMPTS = 3


class BrandResolutionError(RuntimeError):
    """The brand facet could not be read."""


class BrandThrottled(BrandResolutionError):
    """Home Depot is rate limiting. Waiting and retrying is the fix."""


@dataclass(frozen=True)
class BrandMatch:
    """A brand name resolved to a working facet token."""

    name: str
    """Home Depot's own spelling, e.g. "MILWAUKEE" — used as written."""

    token: str
    catalog_count: int | None = None
    """Items the Brand facet claimed, before verification."""

    verified_total: int | None = None
    """Items actually reachable by walking the token. None if unverified."""

    @property
    def config_entry(self) -> str:
        """The `Brand:token` form stored in BRAND_TOKENS."""
        return f"{self.name}:{self.token}"


async def read_brand_facet(
    client: HDClient,
    settings: Settings,
    store_id: str,
    *,
    storefilter: str = "ALL",
) -> dict[str, tuple[str, int | None]]:
    """One read of the catalog's Brand facet.

    Returns {UPPERCASED LABEL: (token, count)}. An empty mapping means the read
    failed or returned no Brand dimension — callers must not treat that as
    "there are no brands".
    """
    total, dimensions = await fetch_facets(
        client, settings, settings.tools_nav_param, store_id, storefilter
    )
    if client.is_throttled:
        raise BrandThrottled(
            "Home Depot is rate limiting requests. Wait a minute and try again."
        )

    refinements = dimensions.get(BRAND_DIMENSION) or []
    out: dict[str, tuple[str, int | None]] = {}
    for ref in refinements:
        label = (ref.get("label") or "").strip()
        token = (ref.get("token") or "").strip()
        if label and token:
            out[label.upper()] = (token, ref.get("count"))

    log.info("Read brand facet", store_id=store_id, brands=len(out), catalog_total=total)
    return out


async def list_brands(
    client: HDClient,
    settings: Settings,
    store_id: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
) -> dict[str, tuple[str, int | None]]:
    """Read the Brand facet, retrying only while it comes back empty.

    An empty read means the request failed, not that the catalog has no
    brands. A non-empty read is returned as-is; callers that need confidence a
    *specific* brand is absent use _search_facet, which keeps looking.
    """
    for attempt in range(1, attempts + 1):
        brands = await read_brand_facet(client, settings, store_id)
        if brands:
            return brands
        log.info("Brand facet came back empty, retrying", attempt=attempt)

    raise BrandResolutionError(
        "Could not read Home Depot's brand list. Check connectivity and try again."
    )


async def _search_facet(
    client: HDClient,
    settings: Settings,
    wanted: str,
    store_id: str,
    attempts: int,
) -> tuple[dict[str, tuple[str, int | None]], tuple[str, int | None] | None]:
    """Look for one brand, re-reading while it is missing.

    Responses degrade silently — the same read has returned 166 brands once and
    225 moments later — so a brand missing from a single response is not
    evidence it is absent. Keeps the widest response seen, for suggestions.

    Returns (widest_seen, entry_or_None).
    """
    widest: dict[str, tuple[str, int | None]] = {}
    for attempt in range(1, attempts + 1):
        brands = await read_brand_facet(client, settings, store_id)
        if len(brands) > len(widest):
            widest = brands
        entry = widest.get(wanted)
        if entry is not None:
            return widest, entry
        if attempt < attempts:
            log.info(
                "Brand absent from this read, retrying in case it was degraded",
                brand=wanted, attempt=attempt, seen=len(brands),
            )

    if not widest:
        raise BrandResolutionError(
            "Could not read Home Depot's brand list. Check connectivity and try again."
        )
    return widest, None


def suggest_brands(name: str, available: dict[str, tuple[str, int | None]], limit: int = 5) -> list[str]:
    """Brand names close to what the user typed, for a 'did you mean' prompt."""
    close = difflib.get_close_matches(name.upper(), list(available), n=limit, cutoff=0.6)
    if close:
        return close
    # difflib misses substring cases like "milwauk" -> "MILWAUKEE".
    needle = name.upper()
    return [b for b in available if needle and needle in b][:limit]


async def verify_token(
    client: HDClient,
    settings: Settings,
    token: str,
    store_id: str,
    *,
    storefilter: str = "ALL",
) -> int | None:
    """Products reachable by walking this token, or None if it resolves to nothing.

    A wrong token is not rejected by the API — it simply returns no total. This
    is what keeps an unusable token out of the config.
    """
    nav = build_nav(settings.root_nav_param, token)
    total, _ = await fetch_facets(client, settings, nav, store_id, storefilter)
    if client.is_throttled:
        raise BrandThrottled(
            "Home Depot is rate limiting requests. Wait a minute and try again."
        )
    return total or None


async def resolve_brand(
    client: HDClient,
    settings: Settings,
    name: str,
    store_id: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    verify: bool = True,
) -> BrandMatch | None:
    """Resolve a brand name to a verified token, or None if there is no such brand.

    Raises BrandThrottled when rate limited and BrandResolutionError when the
    facet cannot be read at all — neither is the same as "no such brand", and
    conflating them would tell a user their brand does not exist when the truth
    is that we could not check.
    """
    wanted = name.strip().upper()
    if not wanted:
        raise BrandResolutionError("A brand name is required")

    brands, entry = await _search_facet(client, settings, wanted, store_id, attempts)
    if entry is None:
        log.info("Brand not in catalog facet", brand=wanted, available=len(brands))
        return None

    token, count = entry
    verified: int | None = None
    if verify:
        verified = await verify_token(client, settings, token, store_id)
        if not verified:
            log.warning("Brand token resolved but returned no products", brand=wanted, token=token)
            return None

    log.info("Resolved brand", brand=wanted, token=token, products=verified or count)
    return BrandMatch(name=wanted, token=token, catalog_count=count, verified_total=verified)
