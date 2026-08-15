"""Product discovery pipeline."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from sqlalchemy import select

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import Product
from hd.hd_api.graphql import search, is_valid_search_response
from hd.hd_api.parsers import parse_products, matches_product_line
from hd.http.client import HDClient
from hd.logging import get_logger
from hd.pipeline.health import check_drift, HealthStatus, emit_health_degraded_alert

log = get_logger("pipeline.discovery")


async def _discover_category(
    client: HDClient,
    settings: Settings,
    brand: str,
    nav_param: str,
    max_pages: int,
    brands: list[str],
    filters: list[str],
    storefilter: str = "ALL",
) -> tuple[int, bool]:
    """Paginate one brand+navParam, filter, upsert. Returns (upsert count, should_abort)."""
    page_upserted = 0

    for page in range(max_pages):
        start_index = page * settings.page_size

        raw = await search(
            client,
            keyword=brand,
            nav_param=nav_param,
            store_id=settings.store_list[0],
            start_index=start_index,
            page_size=settings.page_size,
            storefilter=storefilter,
        )

        # Abort immediately if throttled
        if client.is_throttled:
            log.warning("Throttled during discovery, aborting", keyword=brand)
            return page_upserted, True

        # Validate API response before processing
        if not is_valid_search_response(raw):
            log.error(
                "Invalid API response during discovery",
                brand=brand,
                page=page,
                raw_keys=list(raw.get("data", {}).keys()),
            )
            await emit_health_degraded_alert(
                settings,
                ["API error response on discovery"],
                message="API returned error instead of search results",
            )
            break

        # Check for schema drift
        search_model = raw.get("data", {}).get("searchModel", {})
        raw_products = search_model.get("products", [])

        # Log total results on first page
        if page == 0:
            total = (search_model.get("searchReport") or {}).get("totalProducts")
            if total is not None:
                pages_needed = (total + settings.page_size - 1) // settings.page_size
                log.info(
                    "Discovery scan started",
                    keyword=brand,
                    storefilter=storefilter,
                    total_products=total,
                    pages_needed=pages_needed,
                )

        if not raw_products and page == 0:
            log.warning("Page 0 returned 0 products for brand", brand=brand)

        if raw_products:
            drift_status, failed_paths = check_drift(
                raw_products,
                threshold_pct=settings.drift_failure_threshold_pct,
            )
            if drift_status == HealthStatus.DEGRADED:
                log.error("Schema drift detected", failed_paths=failed_paths)
                await emit_health_degraded_alert(settings, failed_paths)
                return page_upserted, True  # abort

        products = parse_products(raw)
        if not products:
            log.info("No more products on page", brand=brand, page=page)
            break

        # Filter by brand
        products = [
            p for p in products
            if p.brand and p.brand.upper() in [b.upper() for b in brands]
        ]

        # Filter by product line (M12/M18) — only when filters are provided
        if filters:
            products = [
                p for p in products
                if matches_product_line(p, filters)
            ]

        if products:
            count = await _upsert_products(settings, products)
            page_upserted += count
            log.info(
                "Page processed",
                brand=brand,
                page=page,
                found=len(products),
                upserted=count,
            )

        # Stop if we got fewer products than page size
        if len(raw_products) < settings.page_size:
            break

    return page_upserted, False


async def run_discovery(
    settings: Settings,
    brands: list[str] | None = None,
    max_pages: int | None = None,
    clearance_only: bool = False,
    client: HDClient | None = None,
) -> int:
    """Discover products by brand, filter by product line, upsert to DB.

    Returns the total number of products upserted.
    """
    brands = brands or settings.brand_list
    max_pages = max_pages or settings.max_pages
    filters = settings.product_line_filter_list

    nav_param = settings.tools_nav_param
    if clearance_only:
        nav_param = f"{nav_param}Z{settings.clearance_token}"

    scan_keywords = settings.scan_keyword_list
    storefilter = settings.snapshot_storefilter
    owns_client = client is None
    client = client or HDClient(settings)
    total_upserted = 0

    try:
        if scan_keywords:
            # Keyword-split mode: each keyword gets its own discovery pass
            # No product_line_filter — keywords themselves control scope
            for i, keyword in enumerate(scan_keywords):
                log.info("Discovering products", keyword=keyword, storefilter=storefilter)
                count, abort = await _discover_category(
                    client, settings, keyword, nav_param, max_pages, brands, filters=[],
                    storefilter=storefilter,
                )
                total_upserted += count
                log.info("Keyword discovery complete", keyword=keyword, total=count)
                if abort:
                    break
                # Inter-keyword pause (skip after last keyword)
                if i < len(scan_keywords) - 1:
                    pause = random.uniform(
                        settings.keyword_pause_min_seconds,
                        settings.keyword_pause_max_seconds,
                    )
                    await asyncio.sleep(pause)
            # Supplementary ALL pass: discover online-only items IN_STORE misses.
            # Capped at 10 pages per keyword — just enough to find popular online deals.
            if storefilter == "IN_STORE":
                online_max_pages = min(max_pages, 10)
                for i, keyword in enumerate(scan_keywords):
                    log.info("Discovering online products", keyword=keyword)
                    count, abort = await _discover_category(
                        client, settings, keyword, nav_param, online_max_pages, brands, filters=[],
                        storefilter="ALL",
                    )
                    total_upserted += count
                    if count:
                        log.info("Online discovery complete", keyword=keyword, total=count)
                    if abort:
                        break
                    if i < len(scan_keywords) - 1:
                        pause = random.uniform(
                            settings.keyword_pause_min_seconds,
                            settings.keyword_pause_max_seconds,
                        )
                        await asyncio.sleep(pause)

            log.info(
                "Discovery phase complete",
                requests_used=client.request_count,
                budget=client._request_budget,
                products=total_upserted,
            )
        else:
            # Legacy mode: brand list + navParams
            for brand in brands:
                log.info("Discovering products", brand=brand, max_pages=max_pages)
                count, abort = await _discover_category(
                    client, settings, brand, nav_param, max_pages, brands, filters,
                )
                total_upserted += count
                log.info("Brand discovery complete", brand=brand, total=count)
                if abort:
                    return total_upserted

            # Extra navParams (hand tools, etc.) — no product_line_filter
            for extra_nav in settings.extra_nav_param_list:
                np = f"{extra_nav}Z{settings.clearance_token}" if clearance_only else extra_nav
                for brand in brands:
                    log.info("Discovering extra category", brand=brand, nav_param=extra_nav)
                    count, abort = await _discover_category(
                        client, settings, brand, np, max_pages, brands, filters=[],
                    )
                    total_upserted += count
                    log.info("Extra category brand complete", brand=brand, nav_param=extra_nav, total=count)
                    if abort:
                        return total_upserted

    finally:
        if owns_client:
            await client.close()

    return total_upserted


async def _upsert_products(settings: Settings, products: list) -> int:
    """Insert or update products in the database."""
    now = datetime.now(timezone.utc)
    count = 0

    async with get_session(settings) as session:
        for p in products:
            if not p.item_id:
                continue

            result = await session.execute(
                select(Product).where(Product.item_id == p.item_id)
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.last_seen_ts = now
                existing.is_active = True
                if p.title:
                    existing.title = p.title
                if p.canonical_url:
                    existing.canonical_url = p.canonical_url
                if p.model_number:
                    existing.model_number = p.model_number
                if p.image_url:
                    existing.image_url = p.image_url
            else:
                session.add(Product(
                    item_id=p.item_id,
                    brand=p.brand or "Unknown",
                    title=p.title or "Unknown",
                    canonical_url=p.canonical_url,
                    model_number=p.model_number,
                    image_url=p.image_url,
                    first_seen_ts=now,
                    last_seen_ts=now,
                    is_active=True,
                ))

            count += 1

    return count
