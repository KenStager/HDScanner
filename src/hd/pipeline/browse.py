"""Facet-driven brand browse: discovery + snapshots in a single pass.

Why this exists: keyword search scans structurally miss clearance deals.
HD's keyword relevance excludes brand items outside the scanned category
(missed deals lived in Plumbing and Garage while every scan was Tools-scoped),
drops some brand items from keyword result sets entirely, and the API refuses
startIndex > a fixed ceiling, so large result sets can never be fully paged.

Browse mode walks the brand's own category facets instead — the same data the
website's left nav renders. The response's `dimensions` block supplies every
category token with a per-store item count, so coverage is complete,
deterministic, and self-maintaining: category drift shows up in the next facet
read, not as a silent coverage hole.

Two tiers:

- **shelf** (storefilter=IN_STORE): equivalent to the "Pick Up Today" BOPIS
  facet — items physically assorted to the store (~1.4k/store). Small enough
  to sweep fully every run; catches on-shelf clearance.
- **network** (storefilter=ALL): the full brand set (~9k). Catches
  ship-to-store (BOSS) clearance that IN_STORE hides. Too big for one polite
  run, so categories rotate across runs via a cursor file; each category is
  covered completely when its turn comes.

Both tiers upsert products AND append snapshots from the same pages, so a
newly discovered item is monitored the same run it is first seen.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from hd import rotation
from hd.config import Settings
from hd.hd_api.graphql import failure_reason, is_valid_search_response, search
from hd.hd_api.parsers import parse_dimensions, parse_products, parse_snapshots
from hd.http.client import CircuitOpenError, HDClient
from hd.logging import get_logger

log = get_logger("pipeline.browse")

CATEGORY_DIMENSION = "Category"
PRICE_DIMENSION = "Price"


@dataclass
class Walk:
    """One fully-paginatable navParam result set."""

    nav_param: str
    label: str
    total: int
    truncated: bool = False  # True when splitting could not get under the cap
    # The facet read for this node, reusable as page 0 when the walk covers the
    # same navParam. Excluded from equality so a primed walk still compares
    # equal to the plan that produced it.
    primed: dict | None = field(default=None, compare=False, repr=False)


@dataclass
class BrowseSummary:
    products: int = 0
    snapshots: int = 0
    walks: int = 0
    truncated_walks: list[str] = field(default_factory=list)
    aborted: bool = False
    # Categories deliberately deferred to a later run by shelf rotation. Not a
    # failure, but it does mean this run did not see the whole shelf.
    skipped_walks: int = 0


def build_nav(root: str, *tokens: str) -> str:
    """Compose a navParam from the root and facet tokens."""
    parts = [root] + [t for t in tokens if t]
    return "Z".join(parts)


def reachable_cap(settings: Settings) -> int:
    """Largest result set the API allows walking end to end."""
    return settings.api_max_start_index + settings.page_size


def plan_walks(
    nav_param: str,
    label: str,
    total: int | None,
    dimensions: dict[str, list[dict]],
    settings: Settings,
    depth: int = 0,
    price_split_used: bool = False,
) -> tuple[list[Walk], list[tuple[str, str]]]:
    """Split one result set into walkable pieces using its own facets.

    Returns (walks, need_facets) where need_facets lists (nav_param, label)
    nodes that are over the cap and need their own facet fetch to split
    further. Pure function: no I/O — the caller fetches facets and re-plans.
    """
    if total is None or total <= 0:
        return [], []
    cap = reachable_cap(settings)
    if total <= cap:
        return [Walk(nav_param, label, total)], []
    if depth >= settings.browse_max_split_depth:
        log.warning(
            "Facet split depth exhausted — tail beyond API ceiling is unreachable",
            label=label, total=total, cap=cap,
        )
        return [Walk(nav_param, label, total, truncated=True)], []

    existing_tokens = set(nav_param.split("Z"))
    categories = [
        r for r in dimensions.get(CATEGORY_DIMENSION, [])
        if r.get("count") and r["count"] > 0 and r["token"] not in existing_tokens
    ]
    if categories:
        walks: list[Walk] = []
        need: list[tuple[str, str]] = []
        for ref in categories:
            child_nav = build_nav(nav_param, ref["token"])
            child_label = f"{label}/{ref.get('label') or ref['token']}"
            if ref["count"] <= cap:
                walks.append(Walk(child_nav, child_label, ref["count"]))
            else:
                need.append((child_nav, child_label))
        return walks, need

    if not price_split_used:
        prices = [
            r for r in dimensions.get(PRICE_DIMENSION, [])
            if r.get("count") and r["count"] > 0
        ]
        if prices:
            walks = []
            for ref in prices:
                child_nav = build_nav(nav_param, ref["token"])
                child_label = f"{label}/{ref.get('label') or ref['token']}"
                truncated = ref["count"] > cap
                if truncated:
                    log.warning(
                        "Price bucket still over API ceiling — walking reachable head only",
                        label=child_label, total=ref["count"], cap=cap,
                    )
                walks.append(Walk(child_nav, child_label, ref["count"], truncated=truncated))
            return walks, []

    log.warning(
        "No facet available to split oversized set — walking reachable head only",
        label=label, total=total, cap=cap,
    )
    return [Walk(nav_param, label, total, truncated=True)], []


async def fetch_facets(
    client: HDClient,
    settings: Settings,
    nav_param: str,
    store_id: str,
    storefilter: str,
    page_size: int | None = None,
) -> tuple[int | None, dict[str, list[dict]], dict | None]:
    """One facet read: (totalProducts, dimensions, raw response).

    Fetches a full page rather than a single row by default. The counts and
    dimensions are the same either way, but a full page can then serve as page
    0 of the walk this read is planning — and most walks are one page, so
    asking for one row here meant paying for two requests to read one page of
    results. Callers that know the node will be split into differently-navved
    children pass page_size=1, since no page they fetch here is reusable.
    """
    raw = await search(
        client,
        keyword=None,
        nav_param=nav_param,
        store_id=store_id,
        start_index=0,
        page_size=page_size or settings.page_size,
        storefilter=storefilter,
    )
    if not is_valid_search_response(raw):
        log.error(
            "Facet read failed",
            nav_param=nav_param, store_id=store_id, storefilter=storefilter,
            reason=failure_reason(raw) or "api_error",
        )
        return None, {}, None
    search_model = raw.get("data", {}).get("searchModel") or {}
    total = (search_model.get("searchReport") or {}).get("totalProducts")
    return total, parse_dimensions(raw), raw


async def resolve_walks(
    client: HDClient,
    settings: Settings,
    nav_param: str,
    label: str,
    store_id: str,
    storefilter: str,
    total: int | None = None,
    dimensions: dict[str, list[dict]] | None = None,
) -> list[Walk]:
    """Plan walks for a node, fetching facets for any piece still over the cap."""
    raw = None
    if total is None or dimensions is None:
        total, dimensions, raw = await fetch_facets(
            client, settings, nav_param, store_id, storefilter
        )
    walks, need = plan_walks(nav_param, label, total, dimensions, settings)
    _prime(walks, nav_param, raw)
    depth = 1
    while need and depth <= settings.browse_max_split_depth:
        next_need: list[tuple[str, str]] = []
        for child_nav, child_label in need:
            if client.is_throttled:
                return walks
            c_total, c_dims, c_raw = await fetch_facets(
                client, settings, child_nav, store_id, storefilter
            )
            c_walks, c_need = plan_walks(
                child_nav, child_label, c_total, c_dims, settings,
                depth=depth, price_split_used=False,
            )
            _prime(c_walks, child_nav, c_raw)
            walks.extend(c_walks)
            next_need.extend(c_need)
        need = next_need
        depth += 1
    for child_nav, child_label in need:
        log.warning("Unsplit oversized node — walking reachable head only", label=child_label)
        walks.append(Walk(child_nav, child_label, reachable_cap(settings) + 1, truncated=True))
    return walks


# Home Depot's own clock. Matches setup_schedule, which builds the cron slots
# from Eastern wall time for the same reason.
SCHEDULE_TZ = ZoneInfo("America/New_York")


def full_shelf_hours(settings: Settings) -> set[int]:
    hours = set()
    for part in str(getattr(settings, "browse_full_shelf_hours_et", "")).split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.add(int(part))
    return hours


def effective_shelf_fraction(settings: Settings, now: datetime | None = None) -> float:
    """1.0 on a designated full-walk hour, the configured slice otherwise.

    Read once per run rather than per store, so a run that starts at 03:59 and
    crosses into 04:00 keeps the fraction it began with instead of changing
    behaviour halfway through.
    """
    hours = full_shelf_hours(settings)
    if not hours:
        return settings.browse_shelf_fraction
    now = (now or datetime.now(timezone.utc)).astimezone(SCHEDULE_TZ)
    return 1.0 if now.hour in hours else settings.browse_shelf_fraction


def rotate_shelf_walks(
    walks: list[Walk],
    cursors: dict[str, int],
    store_id: str,
    token: str,
    settings: Settings,
    fraction: float | None = None,
) -> tuple[list[Walk], int]:
    """This run's slice of the shelf categories. Returns (picked, skipped).

    The shelf tier re-paginates every category on every run — measured at 51
    walks and 154 page requests, six times a day — while most categories change
    far more slowly than that. Walking a slice per run trades coverage latency
    for request volume: with a fraction of 0.5 a category is revisited every
    other run instead of every run.

    Ordering is by label so the cursor means the same thing between runs even
    as the catalog shifts underneath it.
    """
    fraction = settings.browse_shelf_fraction if fraction is None else fraction
    if fraction >= 1.0 or len(walks) <= 1:
        return walks, 0
    ordered = sorted(walks, key=lambda w: w.label)
    per_run = max(1, math.ceil(len(ordered) * fraction))
    key = f"shelf|{store_id}|{token}"
    start = cursors.get(key, 0) % len(ordered)
    picked = [ordered[(start + i) % len(ordered)] for i in range(per_run)]
    cursors[key] = (start + per_run) % len(ordered)
    return picked, len(ordered) - len(picked)


def _prime(walks: list[Walk], nav_param: str, raw: dict | None) -> None:
    """Hand a facet response to the walk that covers the same navParam.

    A split node's children have their own navParams and get their own facet
    reads, so only an exact match is reusable.
    """
    if raw is None:
        return
    for walk in walks:
        if walk.nav_param == nav_param:
            walk.primed = raw


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:80]


async def walk_and_capture(
    client: HDClient,
    settings: Settings,
    walk: Walk,
    store_id: str,
    storefilter: str,
    seen_item_ids: set[str],
    brands: list[str],
) -> tuple[int, int]:
    """Paginate one walk; upsert products and append snapshots per page.

    Returns (products_upserted, snapshots_inserted). Stops on throttle,
    invalid response, short page, or the API's startIndex ceiling.
    """
    # Imported here to keep module import light and avoid cycles.
    from hd.pipeline.discovery import _upsert_products
    from hd.pipeline.snapshot import _insert_snapshots, _write_raw_json

    products_upserted = 0
    snapshots_inserted = 0
    now = datetime.now(timezone.utc)
    upper_brands = [b.upper() for b in brands]
    page = 0

    while True:
        start_index = page * settings.page_size
        if start_index > settings.api_max_start_index:
            if not walk.truncated:
                log.warning(
                    "Hit API startIndex ceiling mid-walk — remainder not covered",
                    label=walk.label, total=walk.total,
                )
            break

        if page == 0 and walk.primed is not None:
            # Already fetched while planning this walk; consumed once so a
            # later pass cannot serve a stale page.
            raw = walk.primed
            walk.primed = None
        else:
            try:
                raw = await search(
                    client,
                    keyword=None,
                    nav_param=walk.nav_param,
                    store_id=store_id,
                    start_index=start_index,
                    page_size=settings.page_size,
                    storefilter=storefilter,
                )
            except CircuitOpenError:
                # Too many failures too fast. This ends the run, not just this
                # walk, so it has to travel past the per-walk handler.
                raise
            except Exception as e:
                log.error("Browse page fetch failed", label=walk.label, page=page, error=str(e))
                break

        if client.is_throttled:
            break

        if not is_valid_search_response(raw):
            log.error(
                "Browse page unusable — coverage incomplete",
                label=walk.label, store_id=store_id, page=page,
                storefilter=storefilter,
                reason=failure_reason(raw) or "api_error",
            )
            break

        search_model = raw.get("data", {}).get("searchModel") or {}
        raw_products = search_model.get("products") or []

        if page == 0:
            total = (search_model.get("searchReport") or {}).get("totalProducts")
            log.info(
                "Walk started",
                label=walk.label, store_id=store_id, storefilter=storefilter,
                total_products=total, planned=walk.total,
            )

        if settings.store_raw_json:
            await _write_raw_json(
                settings,
                f"browse_{_safe_label(walk.label)}_{storefilter}_p{page}",
                store_id, now, raw,
            )

        products = [
            p for p in parse_products(raw)
            if p.brand and p.brand.upper() in upper_brands
        ]
        if products:
            products_upserted += await _upsert_products(settings, products)

        wanted_ids = {p.item_id for p in products if p.item_id}
        snapshots = [
            s for s in parse_snapshots(raw, store_id)
            if s.item_id in wanted_ids and s.item_id not in seen_item_ids
        ]
        if snapshots:
            snapshots_inserted += await _insert_snapshots(settings, snapshots, store_id, now)
            seen_item_ids.update(s.item_id for s in snapshots)

        if len(raw_products) < settings.page_size:
            break
        page += 1

    return products_upserted, snapshots_inserted


async def _pause(settings: Settings) -> None:
    await asyncio.sleep(random.uniform(
        settings.keyword_pause_min_seconds,
        settings.keyword_pause_max_seconds,
    ))


async def run_browse(
    settings: Settings,
    store_ids: list[str] | None = None,
    client: HDClient | None = None,
    tiers: tuple[str, ...] = ("shelf", "network"),
) -> BrowseSummary:
    """Run the facet-driven browse scan for each store and configured brand."""
    store_ids = store_ids or settings.store_list
    brand_tokens = settings.brand_token_list
    brands = [b for b, _ in brand_tokens]
    owns_client = client is None
    client = client or HDClient(settings, request_budget=settings.browse_request_budget)
    summary = BrowseSummary()
    seen_by_store: dict[str, set[str]] = {s: set() for s in store_ids}

    cursors: dict[str, int] = rotation.load_cursors(settings.browse_cursor_path)
    shelf_fraction = effective_shelf_fraction(settings)

    if not brand_tokens:
        log.error("No brand tokens configured — browse cannot run")
        return summary

    log.info(
        "Browse starting",
        shelf_fraction=shelf_fraction,
        full_walk=shelf_fraction >= 1.0,
        stores=len(store_ids),
    )

    try:
        if "shelf" in tiers:
            for store_id in store_ids:
                for brand, token in brand_tokens:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    walks = await resolve_walks(
                        client, settings, nav, brand, store_id, "IN_STORE",
                    )
                    walks, skipped = rotate_shelf_walks(
                        walks, cursors, store_id, token, settings, shelf_fraction
                    )
                    summary.skipped_walks += skipped
                    for walk in walks:
                        if client.is_throttled:
                            summary.aborted = True
                            break
                        upserts, inserts = await walk_and_capture(
                            client, settings, walk, store_id, "IN_STORE",
                            seen_by_store[store_id], brands,
                        )
                        summary.products += upserts
                        summary.snapshots += inserts
                        summary.walks += 1
                        if walk.truncated:
                            summary.truncated_walks.append(walk.label)
                        await _pause(settings)
                log.info(
                    "Shelf tier complete",
                    store_id=store_id,
                    snapshots=summary.snapshots,
                    requests_used=client.request_count,
                    deferred_categories=summary.skipped_walks or None,
                )

        if "network" in tiers and not client.is_throttled:
            per_run = max(1, settings.browse_network_categories_per_run)
            for store_id in store_ids:
                for brand, token in brand_tokens:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    # The root read only supplies the category list; each
                    # category walks under its own navParam, so there is no
                    # page here to carry forward.
                    total, dims, _ = await fetch_facets(
                        client, settings, nav, store_id, "ALL", page_size=1
                    )
                    categories = sorted(
                        (r for r in dims.get(CATEGORY_DIMENSION, [])
                         if r.get("count") and r["count"] > 0),
                        key=lambda r: (r.get("label") or r["token"]),
                    )
                    if not categories:
                        log.warning(
                            "Network tier: no category facets returned",
                            store_id=store_id, brand=brand,
                        )
                        continue
                    key = f"network|{store_id}|{token}"
                    start = cursors.get(key, 0) % len(categories)
                    picked = [categories[(start + i) % len(categories)] for i in range(min(per_run, len(categories)))]
                    cursors[key] = (start + len(picked)) % len(categories)
                    log.info(
                        "Network tier categories this run",
                        store_id=store_id, brand=brand,
                        categories=[c.get("label") for c in picked],
                    )
                    for ref in picked:
                        if client.is_throttled:
                            summary.aborted = True
                            break
                        child_nav = build_nav(nav, ref["token"])
                        child_label = f"{brand}/{ref.get('label') or ref['token']}"
                        if (ref.get("count") or 0) <= reachable_cap(settings):
                            # Under the cap: walk directly off the parent's count,
                            # no extra facet read needed.
                            walks = await resolve_walks(
                                client, settings, child_nav, child_label,
                                store_id, "ALL", total=ref.get("count"), dimensions={},
                            )
                        else:
                            walks = await resolve_walks(
                                client, settings, child_nav, child_label, store_id, "ALL",
                            )
                        for walk in walks:
                            if client.is_throttled:
                                summary.aborted = True
                                break
                            upserts, inserts = await walk_and_capture(
                                client, settings, walk, store_id, "ALL",
                                seen_by_store[store_id], brands,
                            )
                            summary.products += upserts
                            summary.snapshots += inserts
                            summary.walks += 1
                            if walk.truncated:
                                summary.truncated_walks.append(walk.label)
                            await _pause(settings)
    except CircuitOpenError as e:
        # Reached run_browse uncaught before this, killing the process mid-run:
        # no cursor save, no cooldown written, no summary. Now it stops the run
        # the same way a throttle does.
        log.error("Circuit breaker opened — aborting run", error=str(e))
        summary.aborted = True
    finally:
        rotation.save_cursors(settings.browse_cursor_path, cursors)
        summary.aborted = summary.aborted or client.is_throttled
        log.info(
            "Browse complete",
            products=summary.products,
            snapshots=summary.snapshots,
            walks=summary.walks,
            deferred_categories=summary.skipped_walks or None,
            truncated=summary.truncated_walks or None,
            requests_used=client.request_count,
            throttled=client.is_throttled,
            failures=client.failures or None,
        )
        if owns_client:
            await client.close()

    return summary
