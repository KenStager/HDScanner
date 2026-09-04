"""Daily Deals sweep — price the day's Special Buy set the moment it launches.

Home Depot's daily deals go live at 3:00 ET. The /daily-deals page embeds the
day's exact item list in its Apollo state (`specialBuyMetadata` with
dealType=DAY), including an `endDate` that identifies the set. Each pipeline
run reads that page (one HTTP request); when the set is one we haven't
processed, every listed item is priced through the normal searchModel path
(keyword=itemId resolves a single item) and configured-brand matches are
upserted + snapshotted. A cursor keyed on endDate makes this a once-per-day
sweep: the scheduled run nearest the refresh does it (DAILY_DEALS_HOURS_ET
decides which), and later runs that read the page find it already processed.

`wait_for_refresh` is the sharper instrument: started at 3:00 ET it re-reads
the page every couple of minutes until the end date changes and sweeps the
new set at once, so a deal that sells out before the routine run is still
witnessed at its deal price. Every page read is appended to an evidence file,
never the database, so the list can be compared between reads.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows has no launchd or cron either
    fcntl = None  # type: ignore[assignment]

from hd.config import Settings
from hd.hd_api.graphql import is_valid_search_response, search
from hd.hd_api.parsers import parse_products, parse_snapshots
from hd.http.client import HDClient
from hd.logging import get_logger

log = get_logger("pipeline.daily_deals")

def _page_headers(settings: Settings) -> list[str]:
    """Headers for the daily-deals HTML page.

    Same identity as the API client: one scanner, one name. Accept-Language is
    kept because it is real content negotiation, not disguise.
    """
    from hd.http.client import build_user_agent

    return [
        f"User-Agent: {build_user_agent(settings)}",
        "Accept: text/html,application/xhtml+xml",
        "Accept-Language: en-US,en;q=0.5",
    ]

_APOLLO_MARKER = "window.__APOLLO_STATE__="


@dataclass
class DailyDealSet:
    end_date: str
    item_ids: list[str]
    categories: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DailyDealsSummary:
    end_date: str | None = None
    skipped: bool = False
    items_checked: int = 0
    brand_matches: int = 0
    # Items in the day's set that we already know are not our brands, so were
    # never requested. The sweep used to price all ~110 of them to find out.
    skipped_unknown: int = 0
    products: int = 0
    snapshots: int = 0
    aborted: bool = False
    # Set by wait_for_refresh: how many page reads it took, why it stopped
    # short ("unavailable", "unchanged", "disabled", "cooldown", "late",
    # "locked") and, on a flip, how many seconds after the poll began the new
    # set was first seen.
    polls: int = 0
    stopped: str | None = None
    seconds_to_flip: float | None = None


def parse_daily_deal_page(html: str) -> DailyDealSet | None:
    """Extract the DAY deal set from the page's embedded Apollo state."""
    idx = html.find(_APOLLO_MARKER)
    if idx < 0:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(html[idx + len(_APOLLO_MARKER):])
    except (json.JSONDecodeError, ValueError):
        return None

    root = state.get("ROOT_QUERY") or {}
    for key, value in root.items():
        if not key.startswith("specialBuyMetadata"):
            continue
        if '"dealType":"DAY"' not in key.replace("\\", ""):
            continue
        if not isinstance(value, dict):
            continue
        categories = []
        item_ids: list[str] = []
        for cat in value.get("categoryMetadata") or []:
            if not isinstance(cat, dict):
                continue
            ids = [str(i) for i in cat.get("itemIds") or []]
            categories.append({
                "name": cat.get("name"),
                "tagline": cat.get("tagline"),
                "item_ids": ids,
            })
            item_ids.extend(ids)
        # De-dup preserving order
        seen: set[str] = set()
        unique_ids = [i for i in item_ids if not (i in seen or seen.add(i))]
        return DailyDealSet(
            end_date=str(value.get("endDate") or ""),
            item_ids=unique_ids,
            categories=categories,
        )
    return None


async def fetch_daily_deal_set(settings: Settings) -> DailyDealSet | None:
    """Fetch and parse the daily-deals page. One polite HTTP request."""
    cmd = ["curl", "-s", "-m", "30", "--compressed", settings.daily_deals_url]
    for h in _page_headers(settings):
        cmd.extend(["-H", h])
    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=40,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Daily-deals page fetch failed", error=str(e))
        return None
    if result.returncode != 0 or not result.stdout:
        log.warning("Daily-deals page fetch empty", returncode=result.returncode)
        return None
    deal_set = parse_daily_deal_page(result.stdout)
    if deal_set is None:
        log.warning("Daily-deals page had no parseable deal metadata — page layout may have changed")
    return deal_set


def _read_cursor(path: str) -> str | None:
    try:
        p = Path(path)
        return p.read_text().strip() if p.exists() else None
    except OSError:
        return None


def _write_cursor(path: str, value: str) -> None:
    try:
        Path(path).write_text(value)
    except OSError as e:
        log.warning("Could not persist daily-deals cursor", error=str(e))


def list_digest(item_ids: list[str]) -> str:
    """A short, stable fingerprint of the item list in listed order."""
    return hashlib.sha256("\n".join(item_ids).encode()).hexdigest()[:16]


# What a priced item keeps in the evidence file: every field the snapshot row
# carries, so a write the database refused can be reconstructed from the file.
_PRICE_FIELDS = (
    "store_id", "ts", "price_value", "price_original", "promotion_type",
    "promotion_tag", "savings_center", "dollar_off", "percentage_off",
    "special_buy", "clearance_value", "clearance_dollar_off",
    "clearance_percentage_off", "inventory_qty", "in_stock", "limited_qty",
    "out_of_stock",
)

# Gitignored and never trimmed by the prune job, so the writer caps its own
# growth the way the refusals log does: roll one generation, never fall silent.
_EVIDENCE_MAX_BYTES = 8 * 1024 * 1024


def _price_fields(snapshot: Any) -> dict[str, Any]:
    return {name: getattr(snapshot, name, None) for name in _PRICE_FIELDS}


def record_evidence(
    settings: Settings,
    phase: str,
    deal_set: DailyDealSet | None = None,
    **fields: Any,
) -> None:
    """Append one JSON line to the evidence file. Never lets an error escape.

    The file, not the database, is where a page read goes. Most reads change
    nothing, so the end date, item count and list digest are recorded here
    instead — which is also what shows whether the list changes between
    reads. A priced item is recorded here before its database write, so a
    locked database loses the write and not the observation.
    """
    # The unit suite exercises every phase against fakes. Without this guard
    # those fakes would land in the real evidence file and read as witnessed
    # prices — the refusals log learned the same lesson. A test that means to
    # exercise the writer points it at an absolute temporary path.
    path = Path(settings.daily_deals_evidence_path)
    if os.environ.get("PYTEST_CURRENT_TEST") and not path.is_absolute():
        return
    line: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phase": phase,
    }
    if deal_set is not None:
        line.update(
            end_date=deal_set.end_date,
            items=len(deal_set.item_ids),
            list_sha256=list_digest(deal_set.item_ids),
            categories=[c.get("name") for c in deal_set.categories],
        )
    line.update(fields)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _EVIDENCE_MAX_BYTES:
            path.replace(path.with_suffix(".1.jsonl"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception as e:  # noqa: BLE001 — a diagnostics write never affects a sweep
        log.warning("Could not append daily-deals evidence", error=str(e), phase=phase)


class _SweepLock:
    """One sweep at a time, across processes.

    launchd starts a slot it missed as soon as the machine wakes and coalesces
    several missed slots into one, so the refresh poll and the routine scan
    can begin within the same second. Without this both read the cursor
    before either writes it and price the same set twice — two snapshots
    seconds apart that the diff then compares with each other and announces
    nothing. Non-blocking: the loser skips and says so.
    """

    def __init__(self, settings: Settings) -> None:
        self._path = Path(settings.daily_deals_cursor_path + ".lock")
        self._fh = None

    def acquire(self) -> bool:
        if fcntl is None:
            return True
        try:
            self._fh = self._path.open("a")
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


async def _record_pick(settings: Settings, end_date: str, item_id: str) -> None:
    """Persist a brand match so the dashboard can pin today's set.

    merge() keeps re-runs of the same set idempotent (aborted sweeps resume
    and re-insert) and is portable across SQLite and PostgreSQL.
    """
    from hd.db import base
    from hd.db.models import DailyDealPick

    async with base.get_session(settings) as session:
        await session.merge(DailyDealPick(end_date=end_date, item_id=item_id))


async def _tracked_brand_items(settings: Settings, item_ids: list[str]) -> set[str]:
    """Which of these item ids are already known to be one of our brands.

    The daily-deals page carries item ids and category names but no brand, so
    the sweep used to spend one API request per item to find out — 110 requests
    a day, and across every completed sweep on record it produced zero brand
    matches and zero snapshots. The catalog we already track answers the same
    question for free.
    """
    if not item_ids:
        return set()
    from sqlalchemy import func, select

    from hd.db import base
    from hd.db.models import Product

    uppers = [b.upper() for b in settings.brand_list]
    if not uppers:
        return set()
    async with base.get_session(settings) as session:
        rows = await session.execute(
            select(Product.item_id).where(
                Product.item_id.in_(item_ids),
                func.upper(Product.brand).in_(uppers),
            )
        )
        return {r[0] for r in rows}


async def _catalogued_items(settings: Settings, item_ids: list[str]) -> set[str]:
    """Which of these item ids the catalog has ever seen, whatever the brand.

    `_tracked_brand_items` answers "is it ours", which collapses two different
    unknowns into one: an id the catalog has already answered "not ours" for,
    and an id it has never seen at all. Only the second is a blind spot. The
    first is a question we have already paid for, and re-asking it would spend
    a request to be told what we know.
    """
    if not item_ids:
        return set()
    from sqlalchemy import select

    from hd.db import base
    from hd.db.models import Product

    async with base.get_session(settings) as session:
        rows = await session.execute(
            select(Product.item_id).where(Product.item_id.in_(item_ids))
        )
        return {r[0] for r in rows}


async def run_daily_deals(
    settings: Settings,
    client: HDClient | None = None,
    force: bool = False,
    deal_set: DailyDealSet | None = None,
) -> DailyDealsSummary:
    """Price today's daily-deal items if the set hasn't been processed yet.

    `deal_set` lets a caller that has already read the page (the refresh poll)
    hand the parsed set over instead of fetching it a second time.
    """
    summary = DailyDealsSummary()
    if not settings.daily_deals_enabled and not force:
        summary.skipped = True
        return summary

    if deal_set is None:
        deal_set = await fetch_daily_deal_set(settings)
    if deal_set is None or not deal_set.item_ids:
        record_evidence(settings, "unavailable")
        summary.skipped = True
        return summary
    summary.end_date = deal_set.end_date

    lock = _SweepLock(settings)
    if not lock.acquire():
        log.info("Daily-deals sweep already running elsewhere — skipping", end_date=deal_set.end_date)
        record_evidence(settings, "locked", deal_set)
        summary.skipped = True
        summary.stopped = "locked"
        return summary
    try:
        # Read the cursor under the lock: a sweep that just finished may have
        # written it after this process read the page.
        cursor = _read_cursor(settings.daily_deals_cursor_path)
        if not force and cursor == deal_set.end_date:
            log.info("Daily-deals set already processed", end_date=deal_set.end_date)
            record_evidence(settings, "already_processed", deal_set, cursor=cursor)
            summary.skipped = True
            return summary
        await _sweep(settings, client, deal_set, summary)
    finally:
        lock.release()
    return summary


async def _sweep(
    settings: Settings,
    client: HDClient | None,
    deal_set: DailyDealSet,
    summary: DailyDealsSummary,
) -> None:
    """Price the set's tracked-brand items. Caller holds the sweep lock."""
    # Imported here to keep parity with browse.py and avoid import cycles.
    from hd.pipeline.discovery import _upsert_products
    from hd.pipeline.snapshot import _insert_snapshots

    ref_store = settings.store_list[0] if settings.store_list else None
    if ref_store is None:
        summary.skipped = True
        return

    item_ids = deal_set.item_ids[: settings.daily_deals_max_items]
    if len(deal_set.item_ids) > len(item_ids):
        log.warning(
            "Daily-deals list capped",
            listed=len(deal_set.item_ids), checked=len(item_ids),
        )
    log.info(
        "Daily-deals sweep starting",
        end_date=deal_set.end_date,
        items=len(item_ids),
        categories=[c.get("name") for c in deal_set.categories],
    )

    tracked = await _tracked_brand_items(settings, item_ids)
    catalogued = await _catalogued_items(settings, item_ids)
    # The set splits three ways, and only the third is a blind spot: ours,
    # already answered "not ours", and never seen at all. Recording the sizes
    # costs one database query and no requests, which is what makes it possible
    # to price the probe before buying it.
    never_seen = [i for i in item_ids if i not in catalogued]
    known_not_ours = len(catalogued) - len(tracked)
    record_evidence(
        settings, "partition", deal_set,
        tracked=len(tracked), known_not_ours=known_not_ours,
        never_seen=len(never_seen), probe_budget=max(0, settings.daily_deals_probe_unknown),
    )
    log.info(
        "Daily-deals set partitioned against the catalog",
        listed=len(item_ids), tracked=len(tracked),
        known_not_ours=known_not_ours, never_seen=len(never_seen),
    )
    probe = max(0, settings.daily_deals_probe_unknown)
    targets = [i for i in item_ids if i in tracked] + never_seen[:probe]
    summary.skipped_unknown = len(item_ids) - len(targets)

    if not targets:
        # Nothing in today's set is a brand we track. Record the set as seen so
        # the next run does not re-check it, and spend no requests.
        log.info(
            "Daily-deals set contains none of our brands — no requests made",
            end_date=deal_set.end_date,
            listed=len(item_ids),
            never_seen=len(never_seen),
            categories=[c.get("name") for c in deal_set.categories],
        )
        _write_cursor(settings.daily_deals_cursor_path, deal_set.end_date)
        record_evidence(
            settings, "swept", deal_set,
            tracked=0, api_requests=0, snapshots=0, cursor_saved=True,
        )
        return

    log.info(
        "Daily-deals candidates after brand filter",
        candidates=len(targets), listed=len(item_ids), probing_unknown=min(probe, len(never_seen)),
    )
    item_ids = targets

    owns_client = client is None
    client = client or HDClient(settings, request_budget=len(item_ids) + 10)
    upper_brands = [b.upper() for b in settings.brand_list]
    now = datetime.now(timezone.utc)
    completed = True

    try:
        for item_id in item_ids:
            if client.is_throttled:
                summary.aborted = True
                completed = False
                break
            raw = await search(
                client,
                keyword=item_id,
                nav_param=None,
                store_id=ref_store,
                start_index=0,
                page_size=24,
                storefilter="ALL",
            )
            if not is_valid_search_response(raw):
                completed = False
                continue
            summary.items_checked += 1

            products = [
                p for p in parse_products(raw)
                if p.item_id == item_id
                and p.brand and p.brand.upper() in upper_brands
            ]
            if not products:
                continue
            summary.brand_matches += 1
            snapshots = [
                s for s in parse_snapshots(raw, ref_store)
                if s.item_id == item_id
            ]
            # The observation goes to the evidence file first. If the database
            # is locked or the write fails, the price we saw is still on disk.
            if snapshots:
                record_evidence(
                    settings, "priced", deal_set,
                    item_id=item_id, prices=[_price_fields(s) for s in snapshots],
                )
            else:
                record_evidence(settings, "no_snapshot", deal_set, item_id=item_id)
            await _record_pick(settings, deal_set.end_date, item_id)
            summary.products += await _upsert_products(settings, products)
            if snapshots:
                summary.snapshots += await _insert_snapshots(settings, snapshots, ref_store, now)
    except BaseException:
        # A failed write must not save the cursor: the routine sweep has to
        # see this set as unprocessed and try again.
        completed = False
        raise
    finally:
        cursor_saved = completed and not summary.aborted
        if cursor_saved:
            _write_cursor(settings.daily_deals_cursor_path, deal_set.end_date)
        log.info(
            "Daily-deals sweep complete",
            end_date=deal_set.end_date,
            checked=summary.items_checked,
            brand_matches=summary.brand_matches,
            skipped_unknown=summary.skipped_unknown,
            snapshots=summary.snapshots,
            aborted=summary.aborted,
            cursor_saved=cursor_saved,
        )
        record_evidence(
            settings, "swept", deal_set,
            tracked=len(item_ids), checked=summary.items_checked,
            brand_matches=summary.brand_matches, snapshots=summary.snapshots,
            api_requests=client.request_count, aborted=summary.aborted,
            cursor_saved=cursor_saved,
        )
        if owns_client:
            await client.close()


def _refresh_window(settings: Settings, now_et: datetime) -> bool:
    """Whether `now_et` is close enough to the refresh for polling to make sense.

    A poll that starts hours late — launchd running a slot it missed — would
    find the set already swept by the routine run and spend its reads for
    nothing, or worse, find a pending flip and race that run for it. Outside
    the window the poll takes one read and, if nothing changed, stops.
    """
    from hd.setup_schedule import HD_DEALS_REFRESH

    refresh = now_et.replace(
        hour=HD_DEALS_REFRESH.hour, minute=HD_DEALS_REFRESH.minute, second=0, microsecond=0,
    )
    span = settings.daily_deals_poll_max * settings.daily_deals_poll_seconds
    return refresh - timedelta(minutes=5) <= now_et < refresh + timedelta(seconds=span + 600)


def _start_offset_seconds(settings: Settings) -> int:
    """A per-install delay before the first read, so installs do not all arrive
    on the refresh at the same instant — the same argument scan_minute makes."""
    jitter = max(0, settings.daily_deals_poll_jitter_seconds)
    if jitter == 0:
        return 0
    seed = str(Path.cwd())
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % (jitter + 1)


def _phase_offset_seconds(settings: Settings, now_et: datetime) -> int:
    """Extra delay before the first read, alternating night by night.

    Six reads two minutes apart land on the same six minutes every night, so
    a flip is only ever located to the interval that contains it. Holding the
    series back by one interval-half on alternate nights samples the minutes
    the other phase never sees, which halves the grid the refresh time is
    known on without spending a single extra read.

    The parity comes from the date rather than a counter: a night the machine
    missed cannot flip the sequence, and the phase of any past run can be
    recomputed from its timestamp when the evidence file is read back.
    """
    phase = max(0, settings.daily_deals_poll_phase_seconds)
    if not phase:
        return 0
    return phase if now_et.date().toordinal() % 2 else 0


async def wait_for_refresh(
    settings: Settings,
    client: HDClient | None = None,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now_et: Callable[[], datetime] | None = None,
) -> DailyDealsSummary:
    """Re-read the daily-deals page until its set changes, then sweep it.

    Meant to start at 3:00 Eastern, when Home Depot resets the offers — on
    alternate nights one interval-half after it, so the reads fall on the
    minutes the other phase misses (see `_phase_offset_seconds`). Each
    read that still shows the processed set is recorded and followed by a
    wait; the first read that shows a newer end date is swept at once, so the
    deals are priced within one poll interval of going live instead of an
    hour later. A read that fails or does not parse ends the poll — a refusal
    stops collection, it never escalates it — and the routine sweep on the
    next scheduled run remains the fallback either way.

    With no cursor (a first run) the first read is the baseline, not a flip:
    the page may still carry the expiring set, and sweeping that would write
    a cursor for it and leave the real set to the routine run.
    """
    from hd.http.cooldown import ThrottleCooldown
    from hd.setup_schedule import HD_DEALS_TZ

    summary = DailyDealsSummary()
    if not settings.daily_deals_enabled:
        summary.skipped = True
        summary.stopped = "disabled"
        return summary

    # A 206 from an earlier run holds across processes. The scan defers under
    # it; so does this, before its first page read.
    cooldown = ThrottleCooldown(settings.throttle_cooldown_path, settings.throttle_cooldown_seconds)
    if cooldown.is_active():
        log.warning(
            "Deferring daily-deals poll — Home Depot throttled an earlier run",
            resumes_in_seconds=round(cooldown.remaining_seconds()),
        )
        record_evidence(settings, "cooldown", resumes_in_seconds=round(cooldown.remaining_seconds()))
        summary.skipped = True
        summary.stopped = "cooldown"
        return summary

    clock = now_et or (lambda: datetime.now(HD_DEALS_TZ))
    in_window = _refresh_window(settings, clock())
    cursor = _read_cursor(settings.daily_deals_cursor_path)
    baseline = cursor
    max_polls = max(1, settings.daily_deals_poll_max) if in_window else 1
    jitter = max(0, settings.daily_deals_poll_jitter_seconds)
    # Only inside the window: a poll that starts late is already off its slot,
    # and delaying its single read further would only age the reading.
    phase_offset = _phase_offset_seconds(settings, clock()) if in_window else 0
    offset = (_start_offset_seconds(settings) + phase_offset) if in_window else 0
    if offset:
        await sleep(offset)
    started = datetime.now(timezone.utc)

    for attempt in range(1, max_polls + 1):
        summary.polls = attempt
        deal_set = await fetch_daily_deal_set(settings)
        if deal_set is None or not deal_set.item_ids:
            record_evidence(settings, "unavailable", poll=attempt, cursor=cursor)
            log.warning(
                "Daily-deals page unavailable — poll stopped, no retry",
                poll=attempt,
            )
            summary.skipped = True
            summary.stopped = "unavailable"
            return summary

        if baseline is None:
            # First run: nothing to compare with yet. Remember what the page
            # shows now and watch for it to change.
            baseline = deal_set.end_date
            record_evidence(settings, "baseline", deal_set, poll=attempt)
            unchanged = True
        elif deal_set.end_date < baseline:
            # End dates only move forward. An older one is a stale edge copy,
            # not a flip; sweeping it would move the cursor backwards.
            log.warning(
                "Daily-deals page shows an older set than the cursor — ignoring",
                end_date=deal_set.end_date, cursor=baseline, poll=attempt,
            )
            record_evidence(settings, "older", deal_set, poll=attempt, cursor=baseline)
            unchanged = True
        else:
            unchanged = deal_set.end_date == baseline
            if unchanged:
                record_evidence(settings, "poll", deal_set, poll=attempt, cursor=baseline)

        if unchanged:
            if attempt == max_polls:
                reason = "unchanged" if in_window else "late"
                log.info(
                    "Daily-deals set unchanged — leaving it to the routine sweep",
                    polls=attempt, end_date=deal_set.end_date, reason=reason,
                )
                summary.end_date = deal_set.end_date
                summary.skipped = True
                summary.stopped = reason
                return summary
            await sleep(settings.daily_deals_poll_seconds + (random.uniform(0, jitter) if jitter else 0))
            continue

        elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        record_evidence(
            settings, "flip", deal_set,
            poll=attempt, previous=cursor, seconds_after_start=elapsed,
            start_phase_seconds=phase_offset,
        )
        log.info(
            "Daily-deals set refreshed",
            end_date=deal_set.end_date, previous=cursor,
            poll=attempt, seconds_after_start=elapsed,
        )
        swept = await run_daily_deals(settings, client=client, deal_set=deal_set)
        swept.polls = attempt
        swept.seconds_to_flip = elapsed
        return swept

    return summary  # not reached: the last read always returns above
