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
    both_ends: bool = False  # walk both price ends instead of splitting by facet
    all_brands: bool = False  # capture every brand on the page; the node, not the brand list, is the scope
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


def both_ends_cap(settings: Settings) -> int:
    """Largest result set two price-ordered ends can cover with a safe overlap.

    PRICE ASC and PRICE DESC each reach `reachable_cap` items, so their union
    spans up to 2*cap — minus a margin held back so the seam (where the
    tie-break is not mirrored between the two orderings) always overlaps by
    several pages rather than meeting at a knife edge.
    """
    margin = settings.both_ends_min_overlap_pages * settings.page_size
    return 2 * reachable_cap(settings) - margin


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
    further. Pure apart from one log line per routing decision: no network or
    database I/O — the caller fetches facets and re-plans.
    """
    if total is None or total <= 0:
        return [], []
    cap = reachable_cap(settings)
    band_cap = both_ends_cap(settings)

    def _route(branch: str) -> None:
        # Split parents never write a page 0, so this line is the archive's
        # only record of a parent node's total and route — and the only thing
        # that shows a node drifting into the both-ends band
        # (cap < total <= band_cap).
        log.info("Walk routing", label=label, total=total, branch=branch,
                 cap=cap, band_cap=band_cap, depth=depth)

    if total <= cap:
        _route("single")
        return [Walk(nav_param, label, total)], []
    if settings.both_ends_paging and total <= band_cap:
        # Too big for one walk, small enough for two price ends to cover with
        # margin — walk both ends in one go instead of splitting by facet.
        _route("both-ends")
        return [Walk(nav_param, label, total, both_ends=True)], []
    if depth >= settings.browse_max_split_depth:
        log.warning(
            "Facet split depth exhausted — tail beyond API ceiling is unreachable",
            label=label, total=total, cap=cap,
        )
        _route("truncated-depth")
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
        _route("category-split")
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
            _route("price-split")
            return walks, []

    log.warning(
        "No facet available to split oversized set — walking reachable head only",
        label=label, total=total, cap=cap,
    )
    _route("truncated-no-facet")
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


def parse_et_hours(csv: str) -> set[int]:
    """Parse a CSV of Eastern-time hours (0–23) into a set; ignores junk."""
    hours: set[int] = set()
    for part in str(csv or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.add(int(part))
    return hours


def current_et_hour(now: datetime | None = None) -> int:
    """The current hour on Home Depot's clock — the same clock the schedule uses."""
    return (now or datetime.now(timezone.utc)).astimezone(SCHEDULE_TZ).hour


def full_shelf_hours(settings: Settings) -> set[int]:
    return parse_et_hours(getattr(settings, "browse_full_shelf_hours_et", ""))


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


async def _page_direction(
    client: HDClient,
    settings: Settings,
    walk: Walk,
    store_id: str,
    storefilter: str,
    seen_item_ids: set[str],
    upper_brands: list[str],
    now: datetime,
    *,
    order_by: dict[str, str] | None,
    coverage_ids: set[str],
    expect_ceiling: bool = False,
    coverage_target: int | None = None,
) -> tuple[int, int, int | None]:
    """Page one direction of a walk from startIndex 0 to the API ceiling.

    Records every itemId the API returns (pre-brand-filter) in coverage_ids so a
    both-ends walk can verify its union spanned the node. order_by=None is the
    default BEST_MATCH ordering and the only one that may reuse a primed page — a
    primed facet read is BEST_MATCH and is the wrong page for a price ordering.
    expect_ceiling silences the "remainder not covered" warning for a both-ends
    direction, where reaching the ceiling is the intended half-coverage, not a
    truncation (the union assertion in the caller judges coverage instead).
    coverage_target stops this direction once the shared union has seen every
    item (plus a confirmation buffer), so the second pass need not run to the
    ceiling. Returns (upserts, inserts, total_from_page0).
    """
    # Imported here to keep module import light and avoid cycles.
    from hd.pipeline.discovery import _upsert_products
    from hd.pipeline.snapshot import _insert_snapshots, _write_raw_json

    products_upserted = 0
    snapshots_inserted = 0
    observed_total: int | None = None
    confirm_left: int | None = None   # counts down once coverage first hits target
    page = 0

    while True:
        start_index = page * settings.page_size
        if start_index > settings.api_max_start_index:
            if not walk.truncated and not expect_ceiling:
                log.warning(
                    "Hit API startIndex ceiling mid-walk — remainder not covered",
                    label=walk.label, total=walk.total,
                )
            break

        if page == 0 and order_by is None and walk.primed is not None:
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
                    order_by=order_by,
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
            observed_total = (search_model.get("searchReport") or {}).get("totalProducts")
            log.info(
                "Walk started",
                label=walk.label, store_id=store_id, storefilter=storefilter,
                total_products=observed_total, planned=walk.total,
                order=(order_by or {}).get("order"),
            )

        if settings.store_raw_json:
            # Direction in the filename so a both-ends walk's two passes don't
            # collide: ASC and DESC restart page at 0 and share `now` (one
            # per-walk timestamp), so without this DESC would silently overwrite
            # ASC's early pages in raw_responses. Empty for a single walk, so
            # every existing filename and offline analysis keyed on them is
            # unchanged.
            direction = ""
            if order_by:
                direction = "_asc" if order_by.get("order") == "ASC" else "_desc"
            await _write_raw_json(
                settings,
                f"browse_{_safe_label(walk.label)}_{storefilter}{direction}_p{page}",
                store_id, now, raw,
            )

        parsed = parse_products(raw)
        new_ids_this_page = False
        for p in parsed:
            if p.item_id and p.item_id not in coverage_ids:
                coverage_ids.add(p.item_id)
                new_ids_this_page = True

        products = [
            p for p in parsed
            if p.brand and (walk.all_brands or p.brand.upper() in upper_brands)
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

        # Early stop for the second (DESC) pass: once the union has seen every
        # item, confirm with a few pages that turn up nothing new, then stop
        # instead of running to the ceiling. Resetting the counter whenever a
        # page still yields new itemIds makes the rule "N pages with nothing
        # new", not "N pages" — so an UNDERSTATED totalProducts self-extends
        # rather than leaving a silent gap (the mirror of grew_past_cap, which
        # guards the overstated direction). Completeness then rests on "no new
        # items", never on the count being right.
        if coverage_target is not None and len(coverage_ids) >= coverage_target:
            if confirm_left is None or new_ids_this_page:
                confirm_left = settings.both_ends_confirm_pages
            if confirm_left <= 0:
                break
            confirm_left -= 1

        if len(raw_products) < settings.page_size:
            break
        page += 1

    return products_upserted, snapshots_inserted, observed_total


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
    invalid response, short page, or the API's startIndex ceiling. A both-ends
    walk pages the cheap end and the dear end and asserts their union covered the
    node; a shortfall marks the walk truncated rather than silently under-covering.
    """
    now = datetime.now(timezone.utc)
    upper_brands = [b.upper() for b in brands]
    coverage: set[str] = set()

    if not walk.both_ends:
        up, ins, _ = await _page_direction(
            client, settings, walk, store_id, storefilter, seen_item_ids,
            upper_brands, now, order_by=None, coverage_ids=coverage,
        )
        return up, ins

    # ASC runs full — it can only reach the cheap 744, never the whole node —
    # and hands back the AUTHORITATIVE total it read on page 0, which is the
    # denominator the assertion must use: the node may have grown since planning.
    up_a, ins_a, live_total = await _page_direction(
        client, settings, walk, store_id, storefilter, seen_item_ids,
        upper_brands, now, order_by={"field": "PRICE", "order": "ASC"},
        coverage_ids=coverage, expect_ceiling=True,
    )
    if client.is_throttled:
        # ASC-only is structurally half a node — say so, don't report it clean.
        walk.truncated = True
        return up_a, ins_a

    up_d, ins_d, _ = await _page_direction(
        client, settings, walk, store_id, storefilter, seen_item_ids,
        upper_brands, now, order_by={"field": "PRICE", "order": "DESC"},
        coverage_ids=coverage, expect_ceiling=True,
        coverage_target=live_total,
    )

    # Union assertion — the hard gate, against the LIVE total (planned only as a
    # fallback). A node that grew past the both-ends cap since planning can't be
    # covered by two ends either, so that counts as short too. Surface via
    # walk.truncated, exactly like a facet-split truncation, so a seam gap can
    # never pass silently as full coverage.
    denom = live_total if live_total else walk.total
    covered = len(coverage)
    grew_past_cap = bool(live_total) and live_total > both_ends_cap(settings)
    if grew_past_cap or (denom and covered < denom):
        walk.truncated = True
        log.warning(
            "Both-ends coverage short — seam gap or node grew past cap",
            label=walk.label, covered=covered, total=denom,
            gap=(denom - covered) if denom else None, grew_past_cap=grew_past_cap,
        )
    else:
        log.info(
            "Both-ends coverage complete",
            label=walk.label, covered=covered, total=denom,
        )
    return up_a + up_d, ins_a + ins_d


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
                # Store-wide category nodes (e.g. the grill wall). Deliberately
                # outside shelf rotation — their value is every-pass coverage
                # and they are a few pages each — and captured for ALL brands:
                # the node defines the scope, so the brand filter would silently
                # drop most of what the walk was configured to see.
                for label, token in settings.shelf_category_walk_list:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    walks = await resolve_walks(
                        client, settings, nav, label, store_id, "IN_STORE",
                    )
                    for walk in walks:
                        walk.all_brands = True
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
                    log.info(
                        "Network tier categories this run",
                        store_id=store_id, brand=brand,
                        categories=[c.get("label") for c in picked],
                    )
                    # Advance the cursor only past categories we actually finish,
                    # not past the ones we merely selected. A run cut short by a
                    # 206 or the request budget otherwise skips its unwalked tail
                    # forever, so a slice of the online catalogue would never be
                    # seen. Counting completions lets the next online run resume
                    # exactly where this one stopped — the whole set gets covered.
                    completed = 0
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
                        category_finished = True
                        for walk in walks:
                            if client.is_throttled:
                                summary.aborted = True
                                category_finished = False
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
                        if not category_finished:
                            break
                        completed += 1
                    cursors[key] = (start + completed) % len(categories)
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
