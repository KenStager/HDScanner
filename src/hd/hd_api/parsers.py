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
            media = item.get("media") or {}
            images = media.get("images") or []
            raw_url = images[0].get("url") if images else None
            image_url = raw_url.replace("<SIZE>", "600") if raw_url else None
            products.append(NormalizedProduct(
                item_id=item.get("itemId", ""),
                brand=identifiers.get("brandName"),
                title=identifiers.get("productLabel"),
                canonical_url=identifiers.get("canonicalUrl"),
                model_number=identifiers.get("modelNumber"),
                image_url=image_url,
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
            clearance = pricing.get("clearance") or {}

            inventory = _extract_inventory(item, store_id)

            # Normalize: infer isInStock from quantity when the API omits it
            if inventory and inventory.get("isInStock") is None and inventory.get("quantity") is not None:
                inventory["isInStock"] = inventory["quantity"] > 0

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
                clearance_value=_safe_float(clearance.get("value")),
                clearance_dollar_off=_safe_float(clearance.get("dollarOff")),
                clearance_percentage_off=_safe_int(clearance.get("percentageOff")),
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
    """Navigate the fulfillment path to find inventory for a specific store.

    Prefers BOPIS (buy-online-pickup-in-store) which reflects actual on-shelf
    store inventory, over BOSS (buy-online-ship-to-store) which reports
    warehouse/fulfillment center quantities.
    """
    store_id_str = str(store_id)
    # Collect all matching inventory entries keyed by service type
    candidates: dict[str, dict] = {}
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
                svc_type = (service.get("type") or "").lower()
                locations = service.get("locations") or []
                for location in locations:
                    if location is None:
                        continue
                    if str(location.get("locationId", "")) == store_id_str:
                        inv = location.get("inventory") or {}
                        if svc_type not in candidates:
                            candidates[svc_type] = inv
    except (AttributeError, TypeError):
        pass

    if not candidates:
        return None

    # BOPIS entry existence = item is assorted to this store's shelves.
    # Without BOPIS, express delivery reflects delivery capability, not physical presence.
    if "bopis" in candidates:
        bopis_inv = candidates["bopis"]
        # BOPIS in stock → real shelf inventory
        if not bopis_inv.get("isOutOfStock"):
            return bopis_inv
        # BOPIS OOS but express delivery has stock → clearance item that can't
        # be bought online but is physically present at the store
        if "express delivery" in candidates:
            return candidates["express delivery"]
        return bopis_inv

    # No BOPIS = item not assorted to this store's shelves.
    # Express delivery / BOSS without BOPIS are delivery-only — not on-shelf stock.
    # Mark as OOS to avoid false "in stock" signals.
    for fallback_type in ("express delivery", "boss"):
        if fallback_type in candidates:
            inv = dict(candidates[fallback_type])
            inv["isInStock"] = False
            inv["isOutOfStock"] = True
            inv["quantity"] = None
            return inv

    # Fallback to whatever we found, marked as OOS
    if candidates:
        inv = dict(next(iter(candidates.values())))
        inv["isInStock"] = False
        inv["isOutOfStock"] = True
        inv["quantity"] = None
        return inv

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
