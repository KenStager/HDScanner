"""Snapshot fetching pipeline."""

from __future__ import annotations

import asyncio
import random
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import Product, StoreSnapshot
from hd.hd_api.graphql import search, is_valid_search_response
from hd.hd_api.parsers import parse_snapshots
from hd.http.client import HDClient
from hd.logging import get_logger

log = get_logger("pipeline.snapshot")


async def run_snapshots(
    settings: Settings,
    store_ids: list[str] | None = None,
    limit: int | None = None,
    client: HDClient | None = None,
) -> int:
    """Fetch snapshots for active products at each store.

    Uses paginated category browsing (like discovery) instead of per-product
    API calls. Returns the number of snapshot rows inserted.
    """
    store_ids = store_ids or settings.store_list

    # Load active products
    async with get_session(settings) as session:
        stmt = select(Product).where(Product.is_active.is_(True))
        if limit:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        products = result.scalars().all()

    if not products:
        log.warning("No active products to snapshot")
        return 0

    active_ids = {p.item_id for p in products}
    log.info("Starting snapshots", products=len(products), stores=len(store_ids))

    owns_client = client is None
    client = client or HDClient(settings)
    total_inserted = 0

    try:
        scan_keywords = settings.scan_keyword_list
        storefilter = settings.snapshot_storefilter

        for store_id in store_ids:
            store_count = 0
            seen_item_ids: set[str] = set()  # Prevent duplicate snapshots per store

            if scan_keywords:
                # Keyword-split mode: iterate each keyword with configured storefilter
                for i, keyword in enumerate(scan_keywords):
                    if client.is_throttled:
                        log.warning("Throttled, skipping remaining keywords", store_id=store_id)
                        break
                    kw_count = await _paginate_and_snapshot(
                        client, settings, keyword, store_id, active_ids,
                        seen_item_ids=seen_item_ids,
                        storefilter=storefilter,
                    )
                    store_count += kw_count
                    # Inter-keyword pause (skip after last keyword)
                    if i < len(scan_keywords) - 1:
                        pause = random.uniform(
                            settings.keyword_pause_min_seconds,
                            settings.keyword_pause_max_seconds,
                        )
                        await asyncio.sleep(pause)
            else:
                # Legacy mode: brand list + navParams
                for brand in settings.brand_list:
                    brand_count = await _paginate_and_snapshot(
                        client, settings, brand, store_id, active_ids,
                        seen_item_ids=seen_item_ids,
                    )
                    store_count += brand_count
                for extra_nav in settings.extra_nav_param_list:
                    for brand in settings.brand_list:
                        brand_count = await _paginate_and_snapshot(
                            client, settings, brand, store_id, active_ids,
                            nav_param=extra_nav,
                            seen_item_ids=seen_item_ids,
                        )
                        store_count += brand_count

            log.info("Store snapshots complete", store_id=store_id, rows=store_count)
            total_inserted += store_count

        # Supplementary ALL pass: catch online-only deals that IN_STORE misses.
        # Runs once (first store only) since online pricing is store-independent.
        # Reuses seen_item_ids from the last store so items already captured are skipped.
        if scan_keywords and storefilter == "IN_STORE":
            online_count = 0
            ref_store = store_ids[0]
            for i, keyword in enumerate(scan_keywords):
                kw_count = await _paginate_and_snapshot(
                    client, settings, keyword, ref_store, active_ids,
                    seen_item_ids=seen_item_ids,
                    storefilter="ALL",
                )
                online_count += kw_count
                if i < len(scan_keywords) - 1:
                    pause = random.uniform(
                        settings.keyword_pause_min_seconds,
                        settings.keyword_pause_max_seconds,
                    )
                    await asyncio.sleep(pause)
            if online_count:
                log.info("Online supplement complete", store_id=ref_store, rows=online_count)
            total_inserted += online_count

    finally:
        log.info(
            "Snapshot phase complete",
            requests_used=client.request_count,
            budget=client._request_budget,
            snapshots=total_inserted,
            throttled=client.is_throttled,
        )
        if owns_client:
            await client.close()

    return total_inserted


async def _paginate_and_snapshot(
    client: HDClient,
    settings: Settings,
    brand: str,
    store_id: str,
    active_ids: set[str],
    nav_param: str | None = None,
    seen_item_ids: set[str] | None = None,
    storefilter: str = "ALL",
) -> int:
    """Paginate through category for one brand+store, insert matching snapshots."""
    nav_param = nav_param or settings.tools_nav_param
    inserted = 0
    now = datetime.now(timezone.utc)

    for page in range(settings.max_pages):
        start_index = page * settings.page_size

        try:
            raw = await search(
                client,
                keyword=brand,
                nav_param=nav_param,
                store_id=store_id,
                start_index=start_index,
                page_size=settings.page_size,
                storefilter=storefilter,
            )
        except Exception as e:
            log.error(
                "Snapshot page fetch failed",
                brand=brand, store_id=store_id, page=page, error=str(e),
            )
            break

        # Abort immediately if throttled
        if client.is_throttled:
            break

        if not is_valid_search_response(raw):
            log.error(
                "Invalid API response during snapshot",
                brand=brand,
                store_id=store_id,
                page=page,
            )
            break

        search_model = raw.get("data", {}).get("searchModel", {})
        raw_products = search_model.get("products", [])

        # Log total results on first page for observability
        if page == 0:
            total = (search_model.get("searchReport") or {}).get("totalProducts")
            if total is not None:
                pages_needed = (total + settings.page_size - 1) // settings.page_size
                log.info(
                    "Scan started",
                    keyword=brand,
                    store_id=store_id,
                    storefilter=storefilter,
                    total_products=total,
                    pages_needed=pages_needed,
                )

        # Write raw JSON for the page if configured
        if settings.store_raw_json:
            await _write_raw_json(
                settings, f"page_{brand}_{store_id}_p{page}", store_id, now, raw,
            )

        snapshots = parse_snapshots(raw, store_id)

        # Match against active products, skip already-seen items (dedup)
        matched = [s for s in snapshots if s.item_id in active_ids]
        if seen_item_ids is not None:
            matched = [s for s in matched if s.item_id not in seen_item_ids]
            for s in matched:
                seen_item_ids.add(s.item_id)
        if matched:
            count = await _insert_snapshots(settings, matched, store_id, now)
            inserted += count

        # Stop pagination if last page
        if len(raw_products) < settings.page_size:
            break

    return inserted


async def _insert_snapshots(
    settings: Settings,
    matched_snapshots: list,
    store_id: str,
    now: datetime,
) -> int:
    """Bulk insert matched snapshots."""
    async with get_session(settings) as session:
        for snap in matched_snapshots:
            session.add(StoreSnapshot(
                ts=now,
                store_id=store_id,
                item_id=snap.item_id,
                price_value=Decimal(str(snap.price_value)) if snap.price_value is not None else None,
                price_original=Decimal(str(snap.price_original)) if snap.price_original is not None else None,
                promotion_type=snap.promotion_type,
                promotion_tag=snap.promotion_tag,
                savings_center=snap.savings_center,
                dollar_off=Decimal(str(snap.dollar_off)) if snap.dollar_off is not None else None,
                percentage_off=snap.percentage_off,
                special_buy=snap.special_buy,
                clearance_value=Decimal(str(snap.clearance_value)) if snap.clearance_value is not None else None,
                clearance_dollar_off=Decimal(str(snap.clearance_dollar_off)) if snap.clearance_dollar_off is not None else None,
                clearance_percentage_off=snap.clearance_percentage_off,
                inventory_qty=snap.inventory_qty,
                in_stock=snap.in_stock,
                limited_qty=snap.limited_qty,
                out_of_stock=snap.out_of_stock,
                raw_json=snap.raw,
            ))
    return len(matched_snapshots)


async def _write_raw_json(
    settings: Settings,
    item_id: str,
    store_id: str,
    ts: datetime,
    raw: dict[str, Any],
) -> None:
    """Write raw API response to disk (async I/O)."""
    try:
        raw_dir = Path(settings.raw_json_dir)
        ts_str = ts.strftime("%Y%m%d_%H%M%S")
        filepath = raw_dir / f"{item_id}_{store_id}_{ts_str}.json"
        content = json.dumps(raw, indent=2)
        await asyncio.to_thread(raw_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(filepath.write_text, content)
    except Exception as e:
        log.warning("Failed to write raw JSON", error=str(e))
