"""Parse raw API responses into normalized dataclasses."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hd.hd_api.models import NormalizedProduct, NormalizedSnapshot


def parse_products(raw_response: dict[str, Any]) -> list[NormalizedProduct]:
    """Extract product list from a searchModel response."""
    products = []
    try:
        items = raw_response.get("data", {}).get("searchModel", {}).get("products", [])
    except (AttributeError, TypeError):
        return products

    if not items:
        return products

    for item in items:
        if item is None:
            continue
        try:
            identifiers = item.get("identifiers") or {}
            products.append(NormalizedProduct(
                item_id=item.get("itemId", ""),
                brand=identifiers.get("brandName"),
                title=identifiers.get("productLabel"),
                canonical_url=identifiers.get("canonicalUrl"),
                model_number=identifiers.get("modelNumber"),
            ))
        except (AttributeError, TypeError):
            continue

    return products


def parse_snapshots(
    raw_response: dict[str, Any],
    store_id: str,
) -> list[NormalizedSnapshot]:
    """Extract snapshot data from a searchModel response for a specific store."""
    snapshots = []
    try:
        items = raw_response.get("data", {}).get("searchModel", {}).get("products", [])
    except (AttributeError, TypeError):
        return snapshots

    if not items:
        return snapshots

    now = datetime.now(timezone.utc)

    for item in items:
        if item is None:
            continue
        try:
            item_id = item.get("itemId", "")
            if not item_id:
                continue

            pricing = item.get("pricing") or {}
            promotion = pricing.get("promotion") or {}

            inventory = _extract_inventory(item, store_id)

            snapshots.append(NormalizedSnapshot(
                item_id=item_id,
                store_id=store_id,
                ts=now,
                price_value=_safe_float(pricing.get("value")),
                price_original=_safe_float(pricing.get("original")),
                promotion_type=promotion.get("type"),
                promotion_tag=promotion.get("promotionTag"),
                savings_center=promotion.get("savingsCenter"),
                dollar_off=_safe_float(promotion.get("dollarOff")),
                percentage_off=_safe_int(promotion.get("percentageOff")),
                special_buy=_safe_bool(pricing.get("specialBuy")),
                inventory_qty=inventory.get("quantity") if inventory else None,
                in_stock=inventory.get("isInStock") if inventory else None,
                limited_qty=inventory.get("isLimitedQuantity") if inventory else None,
                out_of_stock=inventory.get("isOutOfStock") if inventory else None,
                raw=item,
            ))
        except (AttributeError, TypeError):
            continue

    return snapshots


def _extract_inventory(item: dict, store_id: str) -> dict | None:
    """Navigate the fulfillment path to find inventory for a specific store."""
    try:
        fulfillment = item.get("fulfillment") or {}
        options = fulfillment.get("fulfillmentOptions") or []
        for option in options:
            if option is None:
                continue
            services = option.get("services") or []
            for service in services:
                if service is None:
                    continue
                locations = service.get("locations") or []
                for location in locations:
                    if location is None:
                        continue
                    if str(location.get("locationId", "")) == str(store_id):
                        return location.get("inventory") or {}
    except (AttributeError, TypeError):
        pass
    return None


def matches_product_line(product: NormalizedProduct, filters: list[str]) -> bool:
    """Check if a product matches any of the product line filters (e.g. M12, M18)."""
    if not filters:
        return True
    searchable = f"{product.title or ''} {product.model_number or ''}".upper()
    return any(f.upper() in searchable for f in filters)


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_bool(val: Any) -> bool | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    return bool(val)
