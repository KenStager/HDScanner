"""Daily Deals sweep — price the day's Special Buy set the moment it launches.

Home Depot's daily deals go live at 3:00 ET. The /daily-deals page embeds the
day's exact item list in its Apollo state (`specialBuyMetadata` with
dealType=DAY), including an `endDate` that identifies the set. Each pipeline
run reads that page (one HTTP request); when the set is one we haven't
processed, every listed item is priced through the normal searchModel path
(keyword=itemId resolves a single item) and configured-brand matches are
upserted + snapshotted. A cursor keyed on endDate makes this a once-per-day
sweep that lands on the first run after 3:00 — the scheduled 3:10 ET run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


async def run_daily_deals(
    settings: Settings,
    client: HDClient | None = None,
    force: bool = False,
) -> DailyDealsSummary:
    """Price today's daily-deal items if the set hasn't been processed yet."""
    summary = DailyDealsSummary()
    if not settings.daily_deals_enabled and not force:
        summary.skipped = True
        return summary

    deal_set = await fetch_daily_deal_set(settings)
    if deal_set is None or not deal_set.item_ids:
        summary.skipped = True
        return summary
    summary.end_date = deal_set.end_date

    if not force and _read_cursor(settings.daily_deals_cursor_path) == deal_set.end_date:
        log.info("Daily-deals set already processed", end_date=deal_set.end_date)
        summary.skipped = True
        return summary

    # Imported here to keep parity with browse.py and avoid import cycles.
    from hd.pipeline.discovery import _upsert_products
    from hd.pipeline.snapshot import _insert_snapshots

    ref_store = settings.store_list[0] if settings.store_list else None
    if ref_store is None:
        summary.skipped = True
        return summary

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
    unknown = [i for i in item_ids if i not in tracked]
    probe = max(0, settings.daily_deals_probe_unknown)
    targets = [i for i in item_ids if i in tracked] + unknown[:probe]
    summary.skipped_unknown = len(item_ids) - len(targets)

    if not targets:
        # Nothing in today's set is a brand we track. Record the set as seen so
        # the next run does not re-check it, and spend no requests.
        log.info(
            "Daily-deals set contains none of our brands — no requests made",
            end_date=deal_set.end_date,
            listed=len(item_ids),
            categories=[c.get("name") for c in deal_set.categories],
        )
        _write_cursor(settings.daily_deals_cursor_path, deal_set.end_date)
        return summary

    log.info(
        "Daily-deals candidates after brand filter",
        candidates=len(targets), listed=len(item_ids), probing_unknown=min(probe, len(unknown)),
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
            await _record_pick(settings, deal_set.end_date, item_id)
            summary.products += await _upsert_products(settings, products)
            snapshots = [
                s for s in parse_snapshots(raw, ref_store)
                if s.item_id == item_id
            ]
            if snapshots:
                summary.snapshots += await _insert_snapshots(settings, snapshots, ref_store, now)
    finally:
        if completed and not summary.aborted:
            _write_cursor(settings.daily_deals_cursor_path, deal_set.end_date)
        log.info(
            "Daily-deals sweep complete",
            end_date=deal_set.end_date,
            checked=summary.items_checked,
            brand_matches=summary.brand_matches,
            skipped_unknown=summary.skipped_unknown,
            snapshots=summary.snapshots,
            aborted=summary.aborted,
            cursor_saved=completed and not summary.aborted,
        )
        if owns_client:
            await client.close()

    return summary
