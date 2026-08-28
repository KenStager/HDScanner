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
from datetime import datetime, timedelta, timezone
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
    # Runtime outcome, filled by walk_and_capture and read by walk_status.
    # Excluded from equality for the same reason as primed: a walked plan
    # still compares equal to the plan that produced it.
    observed_ids: int | None = field(default=None, compare=False)
    live_total: int | None = field(default=None, compare=False)
    # A mid-walk stop that was not the natural end of the results: a fetch
    # error, an unusable page, or a throttle. Distinct from `truncated`,
    # which is a planning-time judgment; walk_status folds both together.
    cut: bool = field(default=False, compare=False)


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
    # Walks not started because the run could not afford to finish them. Also
    # not a failure — the alternative is a truncated walk, which is worse — but
    # kept separate from skipped_walks: rotation defers on a fixed schedule,
    # this defers on budget, and only one of them says the run ran short.
    #
    # WALKS ONLY. Categories deferred before any walk was planned for them go
    # to deferred_categories below. The two cannot be summed — one category
    # resolves to one or many walks — and this counter is the instrument the
    # admission-ceiling experiment is judged on, so a mixed unit here lands on
    # a gate decision.
    deferred_walks: int = 0
    # Categories dropped at PLANNING time, before their walks existed, because
    # the run had already spent its budget. Same cause as deferred_walks, a
    # different unit.
    deferred_categories: int = 0


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


def walk_cost_estimate(walk: Walk, settings: Settings) -> int:
    """Requests this walk will need, before it is started.

    Bounded by what the walk can actually reach, not by the node's total: the
    API refuses startIndex past api_max_start_index, so a walk over the cap
    stops at the reachable head rather than paging the whole node.

    The trailing +1 is a flat safety margin, NOT the facet read — `admits` is
    called after `resolve_walks` has already paid for that, so it is in
    `client.request_count` before this estimate is ever compared. Keep the
    margin: it is what stops a walk that lands exactly on the ceiling from
    being the request that trips the quota. The primed discount is separate
    and real — a walk that reuses page 0 genuinely issues one request fewer —
    so both together still leave the margin intact.

    KNOWN LIMIT, deliberately accepted. For a single walk this is an upper
    bound only while the node's reported total is accurate. `_page_direction`
    keeps paging while pages come back full, so a node that UNDERSTATES its
    total self-extends past this estimate — up to the structural ceiling of
    `per_direction` pages, never beyond. Sizing every walk at that ceiling
    instead would be airtight but would admit only ~7 walks per run against a
    237 ceiling, where runs currently complete 21-47; it would cost far more
    coverage than the overshoot it prevents. The measured basis for accepting
    it: across 118 complete walks in `walk_coverage`, observed == expected
    every time, so no single walk has yet self-extended at all. The ceiling
    keeps margin below the real quota stop to absorb one such overshoot.

    Both-ends is different and is sized at the worst case, because there the
    worst case is routine rather than hypothetical — see below.
    """
    page_size = max(1, settings.page_size)
    total = walk.total or 0
    per_direction = settings.api_max_start_index // page_size + 1
    if walk.both_ends:
        # Worst case, and it is reached routinely. ASC always runs to the API
        # ceiling (a both-ends node is by definition over reachable_cap). DESC
        # stops early only when the union reaches the live total — so whenever
        # the union falls SHORT, DESC also runs to the ceiling. That shortfall
        # is not exotic: it is the "Both-ends coverage short" branch below, and
        # on this catalogue it fires about half the time (seam duplicates, or a
        # total that overstates the distinct itemIds the node will return).
        #
        # Sizing this from the node's item count instead under-estimates by up
        # to a full direction — worst at the BOTTOM of the band, where a 745
        # item node looks like 32 pages and can cost 62. Over-estimating only
        # defers a walk to the next run; under-estimating starts a walk that
        # cannot finish, which is the exact failure this function exists to
        # prevent.
        pages = 2 * per_direction
    else:
        items = min(total, reachable_cap(settings))
        pages = math.ceil(items / page_size)
    # A primed page 0 is only reused on the default BEST_MATCH ordering
    # (_page_direction: `page == 0 and order_by is None`). Both-ends walks
    # always pass a price ordering, so they never consume it and must not be
    # discounted for it.
    discount = 1 if (walk.primed and not walk.both_ends) else 0
    return max(1, pages) + 1 - discount


def admission_ceiling(settings: Settings) -> int:
    """Request count past which no new walk is started. 0 means "no ceiling".

    The tighter of the two positive budgets, never just the explicit one: a
    caller that lowers `browse_request_budget` for a bounded run (the setup
    wizard's install check does exactly this) would otherwise keep a much
    larger configured ceiling, admit walks it cannot pay for, and have them cut
    by budget exhaustion — writing truncated coverage rows out of a health
    check.
    """
    candidates = [v for v in (settings.browse_walk_admission_ceiling,
                              settings.browse_request_budget) if v > 0]
    return min(candidates) if candidates else 0


def budget_spent(client: HDClient, settings: Settings) -> bool:
    """Whether the run has reached the point where it should stop planning too.

    `admits` gates walks, but the planning reads that PRECEDE a walk —
    `fetch_facets` and the facet reads inside `resolve_walks` — are requests
    like any other and count against the same quota. Without this the outer
    loops keep resolving nodes they will then immediately defer, spending the
    very margin the ceiling was bought with, and a quota stop landing on a
    facet read aborts the run just as hard as one landing on a page.
    """
    ceiling = admission_ceiling(settings)
    return ceiling > 0 and client.request_count >= ceiling


def admits(client: HDClient, settings: Settings, walk: Walk) -> bool:
    """Whether this run can still afford to start `walk` and finish it.

    Starting a walk we cannot finish is the expensive mistake: the quota stop
    cuts it mid-page and the coverage row lands "truncated", which is worth
    less than nothing to anything reasoning from absence. Deferring instead
    leaves the walk for a run with room, and writes no row at all.

    The starvation guard covers a walk too large for any single run: deferring
    it every time would mean never walking it. Such a walk is admitted while
    the run still holds at least half its ceiling, so it gets the deepest pass
    available and truncates once rather than never running. With the API's own
    startIndex ceiling bounding every walk to ~2 caps, this branch is currently
    unreachable in practice; it exists so a small ceiling cannot starve a node.
    """
    ceiling = admission_ceiling(settings)
    if ceiling <= 0:
        # 0 means "no budget" everywhere else in this config (see
        # request_budget), so it has to mean "no ceiling" here too. Without
        # this the arithmetic below inverts the intent: remaining goes
        # negative after the first walk and every later one is deferred.
        return True
    remaining = ceiling - client.request_count
    est = walk_cost_estimate(walk, settings)
    if est <= remaining:
        return True
    if est > ceiling:
        return remaining >= ceiling // 2
    return False


def plan_walks(
    nav_param: str,
    label: str,
    total: int | None,
    dimensions: dict[str, list[dict]],
    settings: Settings,
    depth: int = 0,
    price_split_used: bool = False,
    observed_totals: dict[str, int] | None = None,
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
            # The parent's recordCount is a claim, and it is sometimes wrong by
            # multiples: three Tools children were advertised at 603/453/381
            # and reported 2197/1720/1798 at their own page 0 — under the cap
            # by the claim, 3x over it in fact. Routed as single walks, they
            # ran to the API ceiling, covered ~34%, and the cursor moved on as
            # if the category were done. Prefer whatever a walk has actually
            # SEEN at page 0 over what the parent says about it.
            claimed = ref["count"]
            count = max(claimed, (observed_totals or {}).get(child_nav, 0))
            if count > claimed:
                log.info("Node count corrected from observation",
                         label=child_label, claimed=claimed, observed=count)
            if count <= cap:
                walks.append(Walk(child_nav, child_label, count))
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
    observed_totals: dict[str, int] | None = None,
) -> list[Walk]:
    """Plan walks for a node, fetching facets for any piece still over the cap."""
    raw = None
    if total is None or dimensions is None:
        total, dimensions, raw = await fetch_facets(
            client, settings, nav_param, store_id, storefilter
        )
    walks, need = plan_walks(nav_param, label, total, dimensions, settings,
                             observed_totals=observed_totals)
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
                observed_totals=observed_totals,
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
                walk.cut = True
                break

        if client.is_throttled:
            walk.cut = True
            break

        if not is_valid_search_response(raw):
            log.error(
                "Browse page unusable — coverage incomplete",
                label=walk.label, store_id=store_id, page=page,
                storefilter=storefilter,
                reason=failure_reason(raw) or "api_error",
            )
            walk.cut = True
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
            snapshots_inserted += await _insert_snapshots(
                settings, snapshots, store_id, now, walk.nav_param,
            )
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
        up, ins, live_total = await _page_direction(
            client, settings, walk, store_id, storefilter, seen_item_ids,
            upper_brands, now, order_by=None, coverage_ids=coverage,
        )
        walk.live_total = live_total
        walk.observed_ids = len(coverage)
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
        walk.live_total = live_total
        walk.observed_ids = len(coverage)
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
    walk.live_total = live_total
    walk.observed_ids = covered
    return up_a + up_d, ins_a + ins_d


async def _pause(settings: Settings) -> None:
    await asyncio.sleep(random.uniform(
        settings.keyword_pause_min_seconds,
        settings.keyword_pause_max_seconds,
    ))


def walk_status(walk: Walk) -> str:
    """complete | truncated | failed — how far this walk's coverage can be trusted.

    Judged on what was seen, not on how the walk ended: a walk that saw every
    itemId its node claimed is complete even if its final request errored,
    and a walk that ran to a clean stop but saw fewer than the node's own
    total is truncated — catalog churn mid-walk can cause that, and erring
    toward under-claiming is the safe direction. "failed" is reserved for a
    walk that produced nothing usable at all, not even a page-0 total.
    """
    observed = walk.observed_ids or 0
    if observed == 0 and walk.live_total is None:
        return "failed"
    denom = walk.live_total if walk.live_total is not None else walk.total
    if denom and observed >= denom and not walk.truncated:
        return "complete"
    if walk.truncated or walk.cut or (denom and observed < denom):
        return "truncated"
    return "complete"


async def _record_walk(
    settings: Settings,
    run_id: int | None,
    walk: Walk,
    store_id: str,
    storefilter: str,
    started: datetime,
) -> None:
    """Persist one walk's coverage row. Never allowed to break a scan."""
    if run_id is None:
        return
    from hd.db.base import get_session
    from hd.db.models import WalkCoverage

    denom = walk.live_total if walk.live_total is not None else (walk.total or None)
    try:
        async with get_session(settings) as session:
            session.add(WalkCoverage(
                run_id=run_id,
                store_id=store_id,
                tier=storefilter,
                label=walk.label,
                nav_param=walk.nav_param,
                started=started,
                ended=datetime.now(timezone.utc),
                status=walk_status(walk),
                items_expected=denom,
                items_observed=walk.observed_ids or 0,
            ))
    except Exception as e:
        log.error("Coverage record failed", label=walk.label, error=str(e))


async def _record_run_start(settings: Settings, tiers: tuple[str, ...]) -> int | None:
    """Open this run's scan_runs row. Returns its id, or None if it could not
    be written — walks then go unrecorded, which downstream reads as
    unknown-not-complete: the safe failure mode."""
    from hd.db.base import get_session
    from hd.db.models import ScanRun

    try:
        async with get_session(settings) as session:
            run = ScanRun(
                started=datetime.now(timezone.utc),
                tiers=",".join(tiers),
                status="running",
            )
            session.add(run)
            await session.flush()
            return run.id
    except Exception as e:
        log.error("Could not open scan_runs row — walks will be unrecorded", error=str(e))
        return None


async def _record_run_end(
    settings: Settings, run_id: int | None, summary: BrowseSummary, requests_used: int
) -> None:
    if run_id is None:
        return
    from sqlalchemy import update

    from hd.db.base import get_session
    from hd.db.models import ScanRun

    try:
        async with get_session(settings) as session:
            await session.execute(
                update(ScanRun).where(ScanRun.id == run_id).values(
                    ended=datetime.now(timezone.utc),
                    status="aborted" if summary.aborted else "complete",
                    walks=summary.walks,
                    snapshots=summary.snapshots,
                    requests_used=requests_used,
                    # Deferred walks write no coverage row by design, so this
                    # is the only durable trace they leave. Written even when
                    # zero: "this run deferred nothing" is a real observation,
                    # and only a row predating the column reads NULL.
                    deferred_walks=summary.deferred_walks,
                    deferred_categories=summary.deferred_categories,
                )
            )
    except Exception as e:
        log.error("Could not finalize scan_runs row", run_id=run_id, error=str(e))


async def coverage_memory(
    settings: Settings, store_id: str, storefilter: str, refresh_hours: int,
) -> tuple[dict[str, int], set[str]]:
    """What the durable coverage record already knows about this tier's nodes.

    Returns (observed_totals, recently_completed).

    - **observed_totals**: the largest page-0 total ever recorded per node, so a
      parent that under-reports a child cannot route it into a walk that cannot
      finish (see plan_walks).
    - **recently_completed**: nodes walked to `complete` inside the refresh
      window. A category too big for one run defers partway, and because
      `completed` only advances on a finished category the cursor stays put —
      so the next run RE-RESOLVES the same category and walks the same prefix
      again. Measured: MILWAUKEE/Tools was walked 157 times while every other
      Milwaukee category was walked once, and three consecutive runs opened
      with the identical six walks. Skipping what was just completed turns that
      restart into forward progress.

    Keyed on nav_param, never label: `label` falls back to the raw facet token
    when HD omits a label, so the same node can carry two labels across runs.
    Read-only, one query per tier per run, no API cost.
    """
    from sqlalchemy import func, select

    from hd.db import base
    from hd.db.models import WalkCoverage

    totals: dict[str, int] = {}
    recent: set[str] = set()
    try:
        async with base.get_session(settings) as session:
            rows = (await session.execute(
                select(WalkCoverage.nav_param,
                       func.max(WalkCoverage.items_expected),
                       func.max(WalkCoverage.ended).filter(
                           WalkCoverage.status == "complete"))
                .where(WalkCoverage.nav_param.is_not(None),
                       WalkCoverage.store_id == store_id,
                       WalkCoverage.tier == storefilter)
                .group_by(WalkCoverage.nav_param))).all()
    except Exception as e:  # coverage memory is an optimisation, never a gate
        log.warning("Coverage memory unavailable — planning from facets only",
                    error=str(e))
        return {}, set()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0, refresh_hours))
    for nav, max_expected, last_complete in rows:
        if max_expected:
            totals[nav] = int(max_expected)
        if last_complete is not None:
            if last_complete.tzinfo is None:
                last_complete = last_complete.replace(tzinfo=timezone.utc)
            if last_complete >= cutoff:
                recent.add(nav)
    return totals, recent


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

    run_id = await _record_run_start(settings, tiers)

    try:
        if "shelf" in tiers:
            for store_id in store_ids:
                for brand, token in brand_tokens:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    if budget_spent(client, settings):
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    resolved = await resolve_walks(
                        client, settings, nav, brand, store_id, "IN_STORE",
                    )
                    # rotate_shelf_walks advances its cursor over what it
                    # SELECTS. The network tier deliberately advances only over
                    # what it FINISHES, for the reason spelled out there: a run
                    # cut short otherwise skips its unwalked tail until the
                    # cursor wraps. Capture the pre-advance position so the same
                    # guarantee can be restored here if this run stops early.
                    #
                    # Only when rotation actually rotates, though. It returns
                    # early — unsorted and without touching the cursor — on the
                    # same condition tested here, and the cursor indexes the
                    # LABEL-SORTED order. Rewinding against the unsorted list
                    # would write an index meaning a different category than the
                    # one we stopped on, skipping a node the next run should
                    # have walked. That path is live whenever a full-shelf hour
                    # fires (the shipped BROWSE_FULL_SHELF_HOURS_ET default).
                    rotates = shelf_fraction < 1.0 and len(resolved) > 1
                    shelf_key = f"shelf|{store_id}|{token}"
                    shelf_start = (
                        cursors.get(shelf_key, 0) % len(resolved) if resolved else 0
                    )
                    walks, skipped = rotate_shelf_walks(
                        resolved, cursors, store_id, token, settings, shelf_fraction
                    )
                    summary.skipped_walks += skipped
                    # Position of the first walk this run did not ATTEMPT —
                    # deferred on budget, or cut off by a throttle before it
                    # started. None means every selected walk was attempted.
                    # Attempted-but-truncated deliberately does NOT count; see
                    # the note at the truncation branch below.
                    first_incomplete: int | None = None
                    for position, walk in enumerate(walks):
                        if client.is_throttled:
                            summary.aborted = True
                            if first_incomplete is None:
                                first_incomplete = position
                            break
                        if not admits(client, settings, walk):
                            log.info(
                                "Walk deferred — run cannot afford to finish it",
                                label=walk.label, tier="IN_STORE",
                                estimate=walk_cost_estimate(walk, settings),
                                used=client.request_count,
                                ceiling=admission_ceiling(settings),
                            )
                            # Every walk from here on is abandoned, not just
                            # this one — count them all, or the summary
                            # understates what the run did not do.
                            summary.deferred_walks += len(walks) - position
                            if first_incomplete is None:
                                first_incomplete = position
                            break
                        walk_started = datetime.now(timezone.utc)
                        upserts, inserts = await walk_and_capture(
                            client, settings, walk, store_id, "IN_STORE",
                            seen_by_store[store_id], brands,
                        )
                        await _record_walk(
                            settings, run_id, walk, store_id, "IN_STORE", walk_started
                        )
                        summary.products += upserts
                        summary.snapshots += inserts
                        summary.walks += 1
                        if walk.truncated:
                            summary.truncated_walks.append(walk.label)
                            # Deliberately NOT treated as unfinished for the
                            # rewind. A truncated walk ran, captured what it
                            # could, and wrote its (truncated) coverage row —
                            # unlike a deferred walk, which did nothing. Some
                            # nodes truncate every single time they are walked:
                            # a node permanently over the cap with no facet to
                            # split it, or the shelf's both-ends walk, which
                            # goes short on roughly half its runs. Rewinding
                            # onto one pins the cursor there and starves every
                            # category behind it. It comes round again on its
                            # own turn instead.
                        await _pause(settings)
                    if rotates and first_incomplete is not None:
                        # Rewind onto the first walk this run did not finish,
                        # so the next one resumes there instead of rotating
                        # past it.
                        cursors[shelf_key] = (
                            (shelf_start + first_incomplete) % len(resolved)
                        )
                # Store-wide category nodes (e.g. the grill wall). Deliberately
                # outside shelf rotation — their value is every-pass coverage
                # and they are a few pages each — and captured for ALL brands:
                # the node defines the scope, so the brand filter would silently
                # drop most of what the walk was configured to see.
                for label, token in settings.shelf_category_walk_list:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    if budget_spent(client, settings):
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    walks = await resolve_walks(
                        client, settings, nav, label, store_id, "IN_STORE",
                    )
                    for position, walk in enumerate(walks):
                        walk.all_brands = True
                        if client.is_throttled:
                            summary.aborted = True
                            break
                        if not admits(client, settings, walk):
                            log.info(
                                "Walk deferred — run cannot afford to finish it",
                                label=walk.label, tier="IN_STORE",
                                estimate=walk_cost_estimate(walk, settings),
                                used=client.request_count,
                                ceiling=admission_ceiling(settings),
                            )
                            # The break abandons every remaining walk, not
                            # just this one. Counting 1 here understated the
                            # deferral — and this counter is the admission
                            # experiment's instrument, so the understatement
                            # lands directly on a gate decision.
                            summary.deferred_walks += len(walks) - position
                            break
                        walk_started = datetime.now(timezone.utc)
                        upserts, inserts = await walk_and_capture(
                            client, settings, walk, store_id, "IN_STORE",
                            seen_by_store[store_id], brands,
                        )
                        await _record_walk(
                            settings, run_id, walk, store_id, "IN_STORE", walk_started
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
                    rotation_skipped=summary.skipped_walks or None,
                )

        if "network" in tiers and not client.is_throttled:
            per_run = max(1, settings.browse_network_categories_per_run)
            for store_id in store_ids:
                observed_totals, recently_done = await coverage_memory(
                    settings, store_id, "ALL", settings.browse_walk_refresh_hours,
                )
                for brand, token in brand_tokens:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    if budget_spent(client, settings):
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
                        if budget_spent(client, settings):
                            # Stop before paying a facet read for a category
                            # this run cannot walk. completed stays put, so the
                            # cursor leaves it at the head of the next slice.
                            # Count everything still picked: a run that quietly
                            # stops planning would otherwise report the same
                            # summary as one that finished its slice.
                            #
                            # CATEGORIES, not walks — no walk has been planned
                            # for these yet, and a category resolves to one or
                            # many walks. Adding them to deferred_walks mixed
                            # two units in the number the admission-ceiling
                            # gate is judged on.
                            summary.deferred_categories += len(picked) - completed
                            break
                        child_nav = build_nav(nav, ref["token"])
                        child_label = f"{brand}/{ref.get('label') or ref['token']}"
                        if (ref.get("count") or 0) <= reachable_cap(settings):
                            # Under the cap: walk directly off the parent's count,
                            # no extra facet read needed.
                            walks = await resolve_walks(
                                client, settings, child_nav, child_label,
                                store_id, "ALL", total=ref.get("count"), dimensions={},
                                observed_totals=observed_totals,
                            )
                        else:
                            walks = await resolve_walks(
                                client, settings, child_nav, child_label, store_id, "ALL",
                                observed_totals=observed_totals,
                            )
                        # Forward progress. Anything completed inside the
                        # refresh window is already banked, so re-walking it
                        # spends the run without covering anything new — which
                        # is exactly how one oversized category came to be
                        # walked 157 times while its siblings were walked once.
                        planned = len(walks)
                        walks = [w for w in walks if w.nav_param not in recently_done]
                        if planned and not walks:
                            # Every piece is already fresh: the category IS
                            # covered for this pass. Advance past it.
                            log.info("Category already fresh — advancing",
                                     label=child_label, walks=planned)
                            completed += 1
                            continue
                        if len(walks) < planned:
                            log.info("Resuming a part-walked category",
                                     label=child_label, skipped=planned - len(walks),
                                     remaining=len(walks))
                        category_finished = True
                        for position, walk in enumerate(walks):
                            if client.is_throttled:
                                summary.aborted = True
                                category_finished = False
                                break
                            if not admits(client, settings, walk):
                                # Leaves `completed` un-advanced, so this
                                # category is the head of the next run's slice
                                # rather than being rotated past unwalked.
                                log.info(
                                    "Walk deferred — run cannot afford to finish it",
                                    label=walk.label, tier="ALL",
                                    estimate=walk_cost_estimate(walk, settings),
                                    used=client.request_count,
                                    ceiling=admission_ceiling(settings),
                                )
                                # Count every remaining walk, not just this
                                # one — the break abandons them all.
                                summary.deferred_walks += len(walks) - position
                                category_finished = False
                                break
                            walk_started = datetime.now(timezone.utc)
                            upserts, inserts = await walk_and_capture(
                                client, settings, walk, store_id, "ALL",
                                seen_by_store[store_id], brands,
                            )
                            await _record_walk(
                                settings, run_id, walk, store_id, "ALL", walk_started
                            )
                            summary.products += upserts
                            summary.snapshots += inserts
                            summary.walks += 1
                            if walk.truncated:
                                summary.truncated_walks.append(walk.label)
                            await _pause(settings)
                        if not category_finished:
                            # This category stopped mid-walk (admission defer
                            # or throttle) and the break below abandons every
                            # category still in the slice — structurally the
                            # same abandonment as the budget_spent path above,
                            # which does count them. Counting only there left
                            # the rest silently uncounted by EITHER counter,
                            # and this is the abandonment mode admission
                            # control actually produces, so the ~Sept 2 gate
                            # was under-reporting exactly what it measures.
                            # `- 1` because this category was planned and
                            # partly walked; its refused walks are already in
                            # deferred_walks. The two units are never summed,
                            # so this is not double counting.
                            summary.deferred_categories += max(
                                0, len(picked) - completed - 1
                            )
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
        await _record_run_end(settings, run_id, summary, client.request_count)
        log.info(
            "Browse complete",
            products=summary.products,
            snapshots=summary.snapshots,
            walks=summary.walks,
            # NOTE for anyone reading log history across 2026-08-28: the key
            # `deferred_categories` used to carry ROTATION skips and now
            # carries budget deferrals at planning time. Rotation moved to
            # `rotation_skipped`. Lines either side of that date mix two
            # quantities under one key with no marker in the logs themselves.
            rotation_skipped=summary.skipped_walks or None,
            deferred_walks=summary.deferred_walks or None,
            deferred_categories=summary.deferred_categories or None,
            truncated=summary.truncated_walks or None,
            requests_used=client.request_count,
            throttled=client.is_throttled,
            failures=client.failures or None,
        )
        if owns_client:
            await client.close()

    return summary
