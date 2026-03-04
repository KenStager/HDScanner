"""Product discovery pipeline."""

from __future__ import annotations

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


async def run_discovery(
    settings: Settings,
    brands: list[str] | None = None,
    max_pages: int | None = None,
    clearance_only: bool = False,
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

    client = HDClient(settings)
    total_upserted = 0

    try:
        for brand in brands:
            log.info("Discovering products", brand=brand, max_pages=max_pages)
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
                )

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
                raw_products = (
                    raw.get("data", {}).get("searchModel", {}).get("products", [])
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
                        return total_upserted

                products = parse_products(raw)
                if not products:
                    log.info("No more products on page", brand=brand, page=page)
                    break

                # Filter by brand
                products = [
                    p for p in products
                    if p.brand and p.brand.upper() in [b.upper() for b in brands]
                ]

                # Filter by product line (M12/M18)
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

            total_upserted += page_upserted
            log.info("Brand discovery complete", brand=brand, total=page_upserted)

    finally:
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
            else:
                session.add(Product(
                    item_id=p.item_id,
                    brand=p.brand or "Unknown",
                    title=p.title or "Unknown",
                    canonical_url=p.canonical_url,
                    model_number=p.model_number,
                    first_seen_ts=now,
                    last_seen_ts=now,
                    is_active=True,
                ))

            count += 1

    return count
