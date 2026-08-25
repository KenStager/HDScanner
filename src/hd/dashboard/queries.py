"""Async DB query functions for the dashboard data layer.

All functions return dicts (not ORM objects) and accept Settings
to obtain a DB session via the existing base.py pattern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import (
    Alert,
    AlertType,
    DailyDealPick,
    DismissedDeal,
    ItemPriceStat,
    Product,
    Severity,
    Store,
    StoreSnapshot,
)


def _latest_snapshots_subquery():
    """Subquery returning the latest snapshot ts per (store_id, item_id)."""
    return (
        select(
            StoreSnapshot.store_id,
            StoreSnapshot.item_id,
            func.max(StoreSnapshot.ts).label("max_ts"),
        )
        .group_by(StoreSnapshot.store_id, StoreSnapshot.item_id)
        .subquery()
    )


def _first_price_subquery():
    """Subquery returning the first observed price per (store_id, item_id).

    Used to compute a temporal baseline: a product shows an observed discount
    only if its current price is below the first price we ever recorded for it.
    This correctly treats combo-kit structural discounts (where price_original
    is the sum of individual tool prices) as non-events — they show up with
    observed_drop of 0 because the price never actually changed.
    """
    min_ts_sub = (
        select(
            StoreSnapshot.store_id,
            StoreSnapshot.item_id,
            func.min(StoreSnapshot.ts).label("min_ts"),
        )
        .group_by(StoreSnapshot.store_id, StoreSnapshot.item_id)
        .subquery()
    )
    return (
        select(
            StoreSnapshot.store_id,
            StoreSnapshot.item_id,
            StoreSnapshot.price_value.label("first_price"),
        )
        .join(
            min_ts_sub,
            and_(
                StoreSnapshot.store_id == min_ts_sub.c.store_id,
                StoreSnapshot.item_id == min_ts_sub.c.item_id,
                StoreSnapshot.ts == min_ts_sub.c.min_ts,
            ),
        )
        .subquery()
    )


async def get_overview_stats(settings: Settings) -> dict[str, Any]:
    """Return overview statistics for the dashboard home page."""
    async with get_session(settings) as session:
        active_products = (
            await session.execute(
                select(func.count()).select_from(Product).where(Product.is_active.is_(True))
            )
        ).scalar() or 0

        total_snapshots = (
            await session.execute(
                select(func.count()).select_from(StoreSnapshot)
            )
        ).scalar() or 0

        latest_snapshot_ts = (
            await session.execute(
                select(StoreSnapshot.ts).order_by(desc(StoreSnapshot.ts)).limit(1)
            )
        ).scalar_one_or_none()

        cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        alert_count_24h = (
            await session.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.ts >= cutoff_24h)
            )
        ).scalar() or 0

        # Clearance count: latest snapshots carrying an actionable in-store
        # clearance price (pricing.clearance), same rule as alerting — shelf
        # stock, or an online price at/below the clearance price.
        latest_sub = _latest_snapshots_subquery()
        clearance_count = (
            await session.execute(
                select(func.count())
                .select_from(StoreSnapshot)
                .join(
                    latest_sub,
                    and_(
                        StoreSnapshot.store_id == latest_sub.c.store_id,
                        StoreSnapshot.item_id == latest_sub.c.item_id,
                        StoreSnapshot.ts == latest_sub.c.max_ts,
                    ),
                )
                .where(
                    StoreSnapshot.clearance_value.isnot(None),
                    StoreSnapshot.ts
                    >= datetime.now(timezone.utc)
                    - timedelta(hours=settings.deal_freshness_hours),
                    (
                        StoreSnapshot.in_stock.is_(True)
                        | (func.coalesce(StoreSnapshot.inventory_qty, 0) > 0)
                        | (StoreSnapshot.price_value <= StoreSnapshot.clearance_value)
                    ),
                )
            )
        ).scalar() or 0

        # OOS count: latest snapshots with out_of_stock == True
        oos_count = (
            await session.execute(
                select(func.count())
                .select_from(StoreSnapshot)
                .join(
                    latest_sub,
                    and_(
                        StoreSnapshot.store_id == latest_sub.c.store_id,
                        StoreSnapshot.item_id == latest_sub.c.item_id,
                        StoreSnapshot.ts == latest_sub.c.max_ts,
                    ),
                )
                .where(StoreSnapshot.out_of_stock.is_(True))
            )
        ).scalar() or 0

        # Price drops (7d): distinct items with PRICE_DROP alerts
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        price_drops_7d = (
            await session.execute(
                select(func.count(Alert.item_id.distinct()))
                .where(Alert.alert_type == AlertType.PRICE_DROP, Alert.ts >= cutoff_7d)
            )
        ).scalar() or 0

        # Health status reflects the present, not history:
        #   DEGRADED — a HEALTH_DEGRADED alert fired within the last 24h
        #   STALE    — no snapshot in the last 12h (scans should run ~4-hourly)
        #   OK       — otherwise
        degraded = (
            await session.execute(
                select(Alert)
                .where(
                    Alert.alert_type == AlertType.HEALTH_DEGRADED,
                    Alert.ts >= cutoff_24h,
                )
                .order_by(desc(Alert.ts))
                .limit(1)
            )
        ).scalar_one_or_none()

        stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
        snapshot_ts = latest_snapshot_ts
        if snapshot_ts is not None and snapshot_ts.tzinfo is None:
            snapshot_ts = snapshot_ts.replace(tzinfo=timezone.utc)
        if degraded:
            health_status = "DEGRADED"
        elif snapshot_ts is None or snapshot_ts < stale_cutoff:
            health_status = "STALE"
        else:
            health_status = "OK"

    return {
        "active_products": active_products,
        "total_snapshots": total_snapshots,
        "latest_snapshot_ts": latest_snapshot_ts,
        "alert_count_24h": alert_count_24h,
        "clearance_count": clearance_count,
        "oos_count": oos_count,
        "price_drops_7d": price_drops_7d,
        "health_status": health_status,
    }


async def get_products_with_latest(
    settings: Settings, store_ids: list[str]
) -> list[dict[str, Any]]:
    """Return product info joined with the latest snapshot per store."""
    async with get_session(settings) as session:
        latest_sub = _latest_snapshots_subquery()

        # Get all active products
        products_result = await session.execute(
            select(Product).where(Product.is_active.is_(True)).order_by(Product.brand, Product.title)
        )
        products = products_result.scalars().all()

        # Get latest snapshots for all products
        snapshots_result = await session.execute(
            select(StoreSnapshot)
            .join(
                latest_sub,
                and_(
                    StoreSnapshot.store_id == latest_sub.c.store_id,
                    StoreSnapshot.item_id == latest_sub.c.item_id,
                    StoreSnapshot.ts == latest_sub.c.max_ts,
                ),
            )
            .where(StoreSnapshot.store_id.in_(store_ids))
        )
        snapshots = snapshots_result.scalars().all()

        # Index snapshots by (store_id, item_id)
        snap_index: dict[tuple[str, str], StoreSnapshot] = {}
        for s in snapshots:
            snap_index[(s.store_id, s.item_id)] = s

        # Load first observed prices for each (store_id, item_id) to compute
        # observed discounts. The API's percentage_off reflects structural bundle
        # pricing (sum of parts), so we use temporal baselines instead.
        first_sub = _first_price_subquery()
        first_prices_result = await session.execute(
            select(
                first_sub.c.store_id,
                first_sub.c.item_id,
                first_sub.c.first_price,
            )
            .where(first_sub.c.store_id.in_(store_ids))
        )
        first_price_index: dict[tuple[str, str], float] = {}
        for fp_row in first_prices_result.all():
            if fp_row.first_price is not None:
                first_price_index[(fp_row.store_id, fp_row.item_id)] = float(fp_row.first_price)

        rows = []
        for p in products:
            row: dict[str, Any] = {
                "item_id": p.item_id,
                "brand": p.brand,
                "title": p.title,
                "model_number": p.model_number,
                "canonical_url": p.canonical_url,
            }
            for sid in store_ids:
                snap = snap_index.get((sid, p.item_id))
                current_price = float(snap.price_value) if snap and snap.price_value is not None else None
                first_price = first_price_index.get((sid, p.item_id))
                row[f"price_{sid}"] = current_price
                row[f"in_stock_{sid}"] = snap.in_stock if snap else None
                row[f"savings_center_{sid}"] = snap.savings_center if snap else None
                # first_price enables the UI to compute observed_drop without
                # relying on the API's structural percentage_off field
                row[f"first_price_{sid}"] = first_price
            rows.append(row)

        return rows


async def get_product_detail(
    settings: Settings, item_id: str, days_back: int = 90
) -> dict[str, Any]:
    """Return product info with snapshot history and alerts."""
    async with get_session(settings) as session:
        # Product info
        product = (
            await session.execute(
                select(Product).where(Product.item_id == item_id)
            )
        ).scalar_one_or_none()

        if product is None:
            return {"product": None, "snapshots": [], "alerts": [],
                    "store_names": {}, "store_urls": {}, "price_stats": {}}

        # Snapshots within time window, ordered ASC
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        snapshots_result = await session.execute(
            select(StoreSnapshot)
            .where(
                StoreSnapshot.item_id == item_id,
                StoreSnapshot.ts >= cutoff,
            )
            .order_by(StoreSnapshot.ts.asc())
        )
        snapshots = snapshots_result.scalars().all()

        # Alerts for this product
        alerts_result = await session.execute(
            select(Alert)
            .where(Alert.item_id == item_id)
            .order_by(desc(Alert.ts))
        )
        alert_rows = alerts_result.scalars().all()

        # Store names so the page can say "Greenfield", the way the deal board
        # labels its tabs, rather than a bare store number.
        store_rows = (await session.execute(select(Store))).scalars().all()
        store_names = {st.store_id: st.name for st in store_rows if st.name}
        store_urls = {
            st.store_id: url
            for st in store_rows
            if (url := store_page_url(st)) is not None
        }

        # Durable per-store price facts — the record that outlives pruning and
        # the coverage gap. Filtered to configured stores so retired store rows
        # (e.g. a reseeded store id) can't leak onto the page.
        stats_rows = (
            await session.execute(
                select(ItemPriceStat).where(
                    ItemPriceStat.item_id == item_id,
                    ItemPriceStat.store_id.in_(settings.store_list),
                )
            )
        ).scalars().all()
        price_stats = {
            st.store_id: {
                "low_price": float(st.low_price) if st.low_price is not None else None,
                "low_ts": st.low_ts,
                "high_price": float(st.high_price) if st.high_price is not None else None,
                "high_ts": st.high_ts,
                "obs_count": st.obs_count,
                "obs_days": st.obs_days,
                "first_ts": st.first_ts,
                "last_ts": st.last_ts,
            }
            for st in stats_rows
        }

        return {
            "store_names": store_names,
            "store_urls": store_urls,
            "price_stats": price_stats,
            "product": {
                "item_id": product.item_id,
                "brand": product.brand,
                "title": product.title,
                "model_number": product.model_number,
                "canonical_url": product.canonical_url,
                "image_url": product.image_url,
                "first_seen_ts": product.first_seen_ts,
                "last_seen_ts": product.last_seen_ts,
            },
            "snapshots": [
                {
                    "store_id": s.store_id,
                    "ts": s.ts,
                    "price_value": float(s.price_value) if s.price_value is not None else None,
                    "price_original": float(s.price_original) if s.price_original is not None else None,
                    "savings_center": s.savings_center,
                    "percentage_off": s.percentage_off,
                    "special_buy": s.special_buy,
                    "clearance_value": (
                        float(s.clearance_value) if s.clearance_value is not None else None
                    ),
                    "clearance_percentage_off": s.clearance_percentage_off,
                    "inventory_qty": s.inventory_qty,
                    "in_stock": s.in_stock,
                    "out_of_stock": s.out_of_stock,
                }
                for s in snapshots
            ],
            "alerts": [
                {
                    "ts": a.ts,
                    "store_id": a.store_id,
                    "alert_type": a.alert_type.value,
                    "severity": a.severity.value,
                    "payload": a.payload,
                }
                for a in alert_rows
            ],
        }


async def get_alerts(
    settings: Settings,
    *,
    limit: int = 50,
    alert_type: str | None = None,
    severity: str | None = None,
    store_id: str | None = None,
    since_hours: int | None = None,
) -> list[dict[str, Any]]:
    """Return alerts with optional filters, joined with product title."""
    async with get_session(settings) as session:
        stmt = (
            select(Alert, Product.title.label("product_title"), Product.image_url.label("product_image_url"))
            .outerjoin(Product, Alert.item_id == Product.item_id)
        )

        if alert_type:
            try:
                at = AlertType(alert_type)
                stmt = stmt.where(Alert.alert_type == at)
            except ValueError:
                pass

        if severity:
            try:
                sev = Severity(severity)
                stmt = stmt.where(Alert.severity == sev)
            except ValueError:
                pass

        if store_id:
            stmt = stmt.where(Alert.store_id == store_id)

        if since_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            stmt = stmt.where(Alert.ts >= cutoff)

        stmt = stmt.order_by(desc(Alert.ts)).limit(limit)

        result = await session.execute(stmt)
        rows = result.all()

        alerts_out = []
        for row in rows:
            payload = row.Alert.payload or {}
            if row.product_image_url and not payload.get("image_url"):
                payload = {**payload, "image_url": row.product_image_url}
            alerts_out.append({
                "id": row.Alert.id,
                "ts": row.Alert.ts,
                "store_id": row.Alert.store_id,
                "item_id": row.Alert.item_id,
                "alert_type": row.Alert.alert_type.value,
                "severity": row.Alert.severity.value,
                "payload": payload,
                "product_title": row.product_title,
            })
        return alerts_out


async def get_store_summary(settings: Settings) -> list[dict[str, Any]]:
    """Return per-store aggregate statistics."""
    async with get_session(settings) as session:
        # Get all stores
        stores_result = await session.execute(select(Store))
        stores = stores_result.scalars().all()

        latest_sub = _latest_snapshots_subquery()

        # Aggregate stock/clearance counts from latest snapshots.
        # Note: we intentionally do NOT include avg(percentage_off) here because
        # that field reflects structural bundle pricing (sum of individual tool
        # prices), not temporal price reductions. For 402 of 720 products it is
        # permanently > 0, making the average meaningless as a discount signal.
        stmt = (
            select(
                StoreSnapshot.store_id,
                func.count().label("total_products"),
                func.sum(case((StoreSnapshot.in_stock.is_(True), 1), else_=0)).label("in_stock"),
                func.sum(case((StoreSnapshot.out_of_stock.is_(True), 1), else_=0)).label("oos"),
                func.sum(case((StoreSnapshot.savings_center == "CLEARANCE", 1), else_=0)).label("clearance"),
            )
            .join(
                latest_sub,
                and_(
                    StoreSnapshot.store_id == latest_sub.c.store_id,
                    StoreSnapshot.item_id == latest_sub.c.item_id,
                    StoreSnapshot.ts == latest_sub.c.max_ts,
                ),
            )
            .group_by(StoreSnapshot.store_id)
        )
        agg_result = await session.execute(stmt)
        agg_rows = {row.store_id: row for row in agg_result.all()}

        # Count distinct items with confirmed PRICE_DROP alerts in the last 7 days.
        # This is a meaningful, temporally-grounded signal of real price activity.
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        price_drop_result = await session.execute(
            select(
                Alert.store_id,
                func.count(Alert.item_id.distinct()).label("price_drop_count"),
            )
            .where(
                Alert.alert_type == AlertType.PRICE_DROP,
                Alert.ts >= cutoff_7d,
            )
            .group_by(Alert.store_id)
        )
        price_drop_map: dict[str, int] = {
            row.store_id: row.price_drop_count
            for row in price_drop_result.all()
        }

        summaries = []
        for store in stores:
            agg = agg_rows.get(store.store_id)
            summaries.append({
                "store_id": store.store_id,
                "name": store.name,
                "state": store.state,
                "total_products": agg.total_products if agg else 0,
                "in_stock": agg.in_stock if agg else 0,
                "oos": agg.oos if agg else 0,
                "clearance": agg.clearance if agg else 0,
                # Number of distinct items with confirmed price drops in last 7 days.
                # Replaces the misleading avg_discount_pct which was dominated by
                # structural bundle offsets unrelated to actual price changes.
                "price_drops_7d": price_drop_map.get(store.store_id, 0),
            })

        return summaries


async def get_deal_board(settings: Settings) -> dict[str, Any]:
    """Current actionable deals per store for the Deal Board landing page.

    A deal is a latest snapshot with an actionable in-store clearance price
    (same purchasability rule as alerting). Each deal carries what the hunter
    needs to act: image, prices, depth, stock, freshness, and the product URL.
    first_seen_ts is the earliest snapshot that carried this clearance price,
    so "new" means the deal is new — not the product.
    """
    latest_sub = _latest_snapshots_subquery()

    first_deal_sub = (
        select(
            StoreSnapshot.store_id,
            StoreSnapshot.item_id,
            func.min(StoreSnapshot.ts).label("first_deal_ts"),
        )
        .where(StoreSnapshot.clearance_value.isnot(None))
        .group_by(StoreSnapshot.store_id, StoreSnapshot.item_id)
        .subquery()
    )

    async with get_session(settings) as session:
        result = await session.execute(
            select(
                StoreSnapshot,
                Product.title,
                Product.canonical_url,
                Product.image_url,
                first_deal_sub.c.first_deal_ts,
            )
            .join(
                latest_sub,
                and_(
                    StoreSnapshot.store_id == latest_sub.c.store_id,
                    StoreSnapshot.item_id == latest_sub.c.item_id,
                    StoreSnapshot.ts == latest_sub.c.max_ts,
                ),
            )
            .outerjoin(Product, StoreSnapshot.item_id == Product.item_id)
            .outerjoin(
                first_deal_sub,
                and_(
                    StoreSnapshot.store_id == first_deal_sub.c.store_id,
                    StoreSnapshot.item_id == first_deal_sub.c.item_id,
                ),
            )
            .where(
                StoreSnapshot.clearance_value.isnot(None),
                # Freshness: an item unseen by recent scans left the catalog —
                # its old clearance price is no longer a deal.
                StoreSnapshot.ts
                >= datetime.now(timezone.utc) - timedelta(hours=settings.deal_freshness_hours),
                (
                    StoreSnapshot.in_stock.is_(True)
                    | (func.coalesce(StoreSnapshot.inventory_qty, 0) > 0)
                    | (StoreSnapshot.price_value <= StoreSnapshot.clearance_value)
                ),
            )
        )
        rows = result.all()

        store_rows = (await session.execute(select(Store))).scalars().all()
        store_names = {s.store_id: s.name for s in store_rows if s.name}

    now = datetime.now(timezone.utc)
    deals_by_store: dict[str, list[dict[str, Any]]] = {}
    for snap, title, canonical_url, image_url, first_deal_ts in rows:
        clearance = float(snap.clearance_value)
        online = float(snap.price_value) if snap.price_value is not None else None
        pct = snap.clearance_percentage_off
        if pct is None and online and online > 0 and clearance < online:
            pct = round((online - clearance) / online * 100)
        if first_deal_ts is not None and first_deal_ts.tzinfo is None:
            first_deal_ts = first_deal_ts.replace(tzinfo=timezone.utc)
        url = (
            f"https://www.homedepot.com{canonical_url}"
            if canonical_url
            else f"https://www.homedepot.com/s/{snap.item_id}"
        )
        deals_by_store.setdefault(snap.store_id, []).append({
            "item_id": snap.item_id,
            "title": title or f"Item {snap.item_id}",
            "url": url,
            "image_url": image_url,
            "clearance_value": clearance,
            "online_price": online,
            "pct_off": pct or 0,
            "qty": snap.inventory_qty,
            "in_stock": bool(snap.in_stock),
            "first_seen_ts": first_deal_ts,
            "is_new": bool(first_deal_ts and (now - first_deal_ts) <= timedelta(hours=24)),
            "snapshot_ts": snap.ts,
        })

    # Every configured store gets a tab, even with zero deals — absence is data
    for sid in settings.store_list:
        deals_by_store.setdefault(sid, [])

    # Split out user-dismissed deals (they resurface if the deal deepens)
    dismissals = await get_dismissals(settings)
    hidden_by_store: dict[str, list[dict[str, Any]]] = {}
    for sid, deals in deals_by_store.items():
        visible, hidden = [], []
        for d in deals:
            if _is_dismissed(dismissals, sid, d["item_id"], d["clearance_value"]):
                hidden.append(d)
            else:
                visible.append(d)
        deals_by_store[sid] = visible
        hidden_by_store[sid] = hidden

    for deals in deals_by_store.values():
        deals.sort(key=lambda d: (-(d["pct_off"] or 0), d["title"]))

    return {
        "stores": deals_by_store,
        "hidden": hidden_by_store,
        "store_names": store_names,
    }


ONLINE_STORE_KEY = "online"


async def get_dismissals(settings: Settings) -> dict[tuple[str, str], float | None]:
    """Map of (store_id, item_id) -> dismissed price for all dismissed deals."""
    async with get_session(settings) as session:
        rows = (await session.execute(select(DismissedDeal))).scalars().all()
    return {
        (d.store_id, d.item_id): float(d.dismissed_value) if d.dismissed_value is not None else None
        for d in rows
    }


def _is_dismissed(
    dismissals: dict[tuple[str, str], float | None],
    store_id: str,
    item_id: str,
    current_value: float | None,
) -> bool:
    """Hidden while the current deal price is at or above the dismissed price.

    A deal that got deeper since dismissal is a new situation — it resurfaces.
    A dismissal recorded without a price hides the item unconditionally.
    """
    key = (store_id, item_id)
    if key not in dismissals:
        return False
    dismissed_value = dismissals[key]
    if dismissed_value is None or current_value is None:
        return True
    return current_value >= dismissed_value


async def dismiss_deal(
    settings: Settings,
    store_id: str,
    item_id: str,
    value: float | None,
    reason: str | None = None,
) -> None:
    """Mark a deal as not real. Replaces any prior dismissal for the pair."""
    async with get_session(settings) as session:
        existing = (
            await session.execute(
                select(DismissedDeal).where(
                    DismissedDeal.store_id == store_id,
                    DismissedDeal.item_id == item_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.dismissed_value = Decimal(str(value)) if value is not None else None
            existing.reason = reason
            existing.ts = datetime.now(timezone.utc)
        else:
            session.add(DismissedDeal(
                store_id=store_id,
                item_id=item_id,
                dismissed_value=Decimal(str(value)) if value is not None else None,
                reason=reason,
            ))


async def restore_deal(settings: Settings, store_id: str, item_id: str) -> None:
    """Un-dismiss a deal so it shows again."""
    async with get_session(settings) as session:
        existing = (
            await session.execute(
                select(DismissedDeal).where(
                    DismissedDeal.store_id == store_id,
                    DismissedDeal.item_id == item_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            await session.delete(existing)


# The online grid's job is "best of the best": deals our own record can vouch
# for lead, ranked by our measured depth rather than HD's claim. Claim-only
# deals are not excluded — the record is young and most items simply haven't
# been watched yet — but they rotate through a reserved block of slots behind
# the evidence-backed tier, and graduate (or die) as their history develops.
ONLINE_DISPLAY_LIMIT = 60
# Rotation reserve: slots the unverified tier always keeps, so newly appearing
# claims surface while their evidence is still developing instead of being
# crowded out by a settled verified tier.
ONLINE_UNVERIFIED_SLOTS = 15
# The warning strip under the grid: deals we watched sell for less. A current
# price can still be the best available today, so they stay visible — but as
# a small labeled strip, not competitors for grid slots.
ONLINE_WARNING_SLOTS = 6
# Deals no snapshot has ever shown to be buyable: no fulfillment data from any
# recent observation, usually items HD lists in browse results it will not
# actually sell. They stay on the board — missing data is not confirmed OOS —
# but in a small block at the grid's tail, behind every purchasable deal.
ONLINE_UNKNOWN_SLOTS = 6
# A claim-only card whose price we have watched this many distinct days
# without a single move is disproven — the "was" price never existed while we
# were looking — and leaves the board entirely.
HOLLOW_CLAIM_MIN_DAYS = 10
# Minimum measured depth for the evidence-backed tier
VERIFIED_MIN_PCT = 10


def deal_tier(deal: dict[str, Any]) -> str:
    """Classify a candidate by the strength of our own evidence.

    'verified'   — our record vouches for a real discount: a measured drop in
                   the recent window, or the price sits at our witnessed low
                   with a witnessed high meaningfully above it. May carry a
                   dated note of an older, lower price we once recorded.
    'warned'     — we watched it sell for less RECENTLY — or ever, when the
                   claim has no measured support of its own.
    'hollow'     — claim-only, and we watched the price long enough to say the
                   claimed "was" never existed. Dropped from the board.
    'unverified' — HD's claim is all there is, and our record is too young to
                   corroborate or contradict it.

    A warning must be actionable-fresh; a claim-contradiction does not age.
    The witnessed low is durable and never expires, so without a recency
    bound every recurring promo eventually sits above some ancient dip and
    the warning channel numbs. A RECENT low warns whatever we measured (the
    reader could plausibly have had, and may again get, the better price).
    An OLD low still warns when the claim has no measured backing — a May
    low disproves an August "was" just as well as a fresh one — but it does
    not overrule a real measured drop: that deal is verified, and the card
    keeps the old low visible as a dated context chip rather than dressing
    a true discount as a caution.
    """
    low = deal.get("low_price")
    if (
        low is not None and deal.get("price_varied") and deal.get("low_is_older")
        and deal["price"] > low
    ):
        # The exception must clear three gates, each defaulting to warned:
        # the low is dated AND stale ("is not False": an absent or None
        # recency verdict counts as recent — a record that cannot date its
        # low keeps the warning); the drop is measured to the shared
        # threshold; and the evidence that beats the low POSTDATES it —
        # expiring the fact that hurts a deal while keeping an older fact
        # that flatters it would be a thumb on the scale, not a recency
        # rule.
        if (deal.get("low_is_recent", True) is not False
                or (deal.get("evidence_pct") or 0) < VERIFIED_MIN_PCT
                or not deal.get("evidence_outdates_low", False)):
            return "warned"
    if (deal.get("evidence_pct") or 0) >= VERIFIED_MIN_PCT:
        return "verified"
    if (
        not deal.get("price_varied")
        and (deal.get("obs_days") or 0) >= HOLLOW_CLAIM_MIN_DAYS
    ):
        return "hollow"
    return "unverified"


def store_page_url(store: Store) -> str | None:
    """Home Depot's page for one store, or None if we lack the parts.

    Verified format: /l/<name>/<state>/<city>/<zip>/<store_id>. The site
    localizes by cookie and honours no store query parameter, so this page —
    and its "Shop This Store" button — is the only way to point a browser at a
    specific store. City falls back to the store name, which is correct for
    stores whose name is their city.
    """
    city = store.city or store.name
    if not (store.name and store.state and city and store.zip and store.store_id):
        return None
    parts = [store.name, store.state, city, store.zip, store.store_id]
    return "https://www.homedepot.com/l/" + "/".join(
        quote(str(p).replace(" ", "-"), safe="") for p in parts
    )


def _is_older_day(low_ts, snapshot_ts) -> bool:
    """Was the low set on an earlier day than the reading we are showing?

    An item observed once is at its all-time low by definition, which is not a
    fact worth a badge. Requiring an earlier day keeps "lowest recorded" to
    items whose price we actually watched hold.
    """
    if low_ts is None or snapshot_ts is None:
        return False
    return low_ts.date() < snapshot_ts.date()


def _as_utc(dt):
    """Normalize a possibly-naive DB timestamp to aware UTC for comparison."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_recent(low_ts, snapshot_ts, recency_days: int) -> bool:
    """Is the witnessed low fresh enough to still gate today's verdict?

    Feeds deal_tier's warned/verified boundary: a recent lower price is
    actionable and keeps its warning teeth; an old one is history, and a
    real measured drop outranks it (the card then shows it as a dated
    context chip instead of a warning). Unknown ages count as recent, so a
    record that cannot date its low keeps the warning.
    """
    if low_ts is None or snapshot_ts is None:
        return True
    if low_ts.tzinfo is None:
        low_ts = low_ts.replace(tzinfo=timezone.utc)
    if snapshot_ts.tzinfo is None:
        snapshot_ts = snapshot_ts.replace(tzinfo=timezone.utc)
    return (snapshot_ts - low_ts) <= timedelta(days=recency_days)


def _promo_predicate():
    """A snapshot that advertises any kind of discount."""
    return (
        StoreSnapshot.special_buy.is_(True)
        | (func.coalesce(StoreSnapshot.percentage_off, 0) > 0)
        | (StoreSnapshot.price_original > StoreSnapshot.price_value)
    )


def _online_rows_select(settings: Settings, ref_store: str, *,
                        item_ids: list[str] | None = None,
                        require_promo: bool = True):
    """The online board's row query: latest snapshot per item at the reference
    store, joined with everything an honest verdict needs — the window
    baseline, when the promo first appeared, and the durable price stats.

    item_ids narrows to a fixed set (the daily-deals strip). require_promo
    drops the advertised-discount predicate: a pinned item earns a verdict
    whether or not HD is claiming anything for it today.
    """
    latest_sub = _latest_snapshots_subquery()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.deal_history_window_days)
    baseline_sub = (
        select(
            StoreSnapshot.item_id,
            func.max(StoreSnapshot.price_value).label("high_window"),
            func.min(StoreSnapshot.ts).label("first_ts"),
        )
        .where(
            StoreSnapshot.store_id == ref_store,
            StoreSnapshot.ts >= cutoff,
            StoreSnapshot.price_value.isnot(None),
        )
        .group_by(StoreSnapshot.item_id)
        .subquery()
    )
    # When the promo first appeared — "new" means the deal is new, not the
    # product, mirroring the in-store board. Drives the rotation the board
    # needs: fresh deals surface, long-standing ones don't read as news.
    promo_first_sub = (
        select(
            StoreSnapshot.item_id,
            func.min(StoreSnapshot.ts).label("promo_first_ts"),
        )
        .where(
            StoreSnapshot.store_id == ref_store,
            _promo_predicate(),
        )
        .group_by(StoreSnapshot.item_id)
        .subquery()
    )

    stmt = (
        select(
            StoreSnapshot,
            Product.title,
            Product.canonical_url,
            Product.image_url,
            baseline_sub.c.high_window,
            baseline_sub.c.first_ts,
            promo_first_sub.c.promo_first_ts,
            ItemPriceStat.low_price,
            ItemPriceStat.low_ts,
            ItemPriceStat.high_price,
            ItemPriceStat.obs_days,
            ItemPriceStat.high_ts,
            ItemPriceStat.first_ts,
        )
        .join(
            latest_sub,
            and_(
                StoreSnapshot.store_id == latest_sub.c.store_id,
                StoreSnapshot.item_id == latest_sub.c.item_id,
                StoreSnapshot.ts == latest_sub.c.max_ts,
            ),
        )
        .outerjoin(Product, StoreSnapshot.item_id == Product.item_id)
        .outerjoin(baseline_sub, StoreSnapshot.item_id == baseline_sub.c.item_id)
        .outerjoin(promo_first_sub, StoreSnapshot.item_id == promo_first_sub.c.item_id)
        .outerjoin(
            ItemPriceStat,
            and_(
                ItemPriceStat.store_id == StoreSnapshot.store_id,
                ItemPriceStat.item_id == StoreSnapshot.item_id,
            ),
        )
        .where(
            StoreSnapshot.store_id == ref_store,
            StoreSnapshot.price_value.isnot(None),
            # Freshness: unseen by recent scans = gone from the catalog
            StoreSnapshot.ts
            >= datetime.now(timezone.utc)
            - timedelta(hours=settings.deal_freshness_hours),
        )
    )
    if require_promo:
        stmt = stmt.where(_promo_predicate())
    if item_ids is not None:
        stmt = stmt.where(StoreSnapshot.item_id.in_(item_ids))
    return stmt


async def _fulfillment_verdicts(session, settings: Settings, ref_store: str,
                                rows) -> dict[str, bool | None]:
    """Best fulfillment verdict per item: True / False / None (unknown).

    HD's browse responses set fulfillment.fulfillmentOptions to null on some
    rows — an unknown, never a confirmed out-of-stock — and several request
    shapes feed the same snapshot stream, so a null row from one can land on
    top of a fulfillment-bearing row another wrote hours earlier. Before
    calling an item's availability unknown, consult its other snapshots
    inside the same freshness window for the most recent real verdict.
    """
    from hd.hd_api.parsers import has_any_fulfillment

    fulfillment_by_item: dict[str, bool | None] = {
        row[0].item_id: has_any_fulfillment(row[0].raw_json) for row in rows
    }
    unknown_ids = [i for i, v in fulfillment_by_item.items() if v is None]
    if unknown_ids:
        lookback = (
            await session.execute(
                select(StoreSnapshot.item_id, StoreSnapshot.raw_json)
                .where(
                    StoreSnapshot.store_id == ref_store,
                    StoreSnapshot.item_id.in_(unknown_ids),
                    StoreSnapshot.ts
                    >= datetime.now(timezone.utc)
                    - timedelta(hours=settings.deal_freshness_hours),
                )
                .order_by(StoreSnapshot.ts.desc())
            )
        ).all()
        for item_id, raw in lookback:
            if fulfillment_by_item[item_id] is None:
                fulfillment_by_item[item_id] = has_any_fulfillment(raw)
    return fulfillment_by_item


def _deals_from_rows(rows, fulfillment_by_item: dict[str, bool | None],
                     dismissals: dict, settings: Settings) -> list[dict[str, Any]]:
    """Turn row tuples from _online_rows_select into enriched deal dicts.

    Applies the shared evidence math (claimed vs measured depth, history
    gating, witnessed anchors) and the availability verdict, and attaches the
    tier. The only rows dropped are confirmed out-of-stock; display policy —
    depth cutoffs, hollow removal, slot caps — belongs to the callers.
    """
    now = datetime.now(timezone.utc)
    min_history = timedelta(days=settings.price_history_min_days)

    deals: list[dict[str, Any]] = []
    for (snap, title, canonical_url, image_url, high_window, first_ts,
         promo_first_ts, low_price, low_ts, all_high, obs_days,
         high_ts, stats_first_ts) in rows:
        # A price is not a deal if nothing can actually be bought: drop items
        # whose fulfillment data confirms every path is out of stock. An item
        # with no verdict at all stays — missing data is not evidence of
        # unavailability — but is flagged so it can never outrank a deal we
        # know is purchasable.
        availability = fulfillment_by_item.get(snap.item_id)
        if availability is False:
            continue
        value = float(snap.price_value)
        original = float(snap.price_original) if snap.price_original is not None else None

        claimed = snap.percentage_off or 0
        if not claimed and original and original > value:
            claimed = round((original - value) / original * 100)

        # Only issue a history verdict when the history is old enough to mean
        # something — a just-discovered item has no basis for "flat price".
        if first_ts is not None and first_ts.tzinfo is None:
            first_ts = first_ts.replace(tzinfo=timezone.utc)
        observed = (now - first_ts) if first_ts is not None else None
        has_history = observed is not None and observed >= min_history
        high = float(high_window) if has_history and high_window is not None else None
        history_days = observed.days if has_history else None
        true_pct = 0
        if high and high > value:
            true_pct = round((high - value) / high * 100)

        # Witnessed depth: today's price against the highest price we ever saw
        # this item sell for — a dated fact from item_price_stats, immune to
        # the coverage gap that starves the window verdict. Only meaningful
        # when the price actually varied under our watching.
        witnessed_pct = 0
        if (
            low_price is not None and all_high is not None
            and low_price != all_high and float(all_high) > value
        ):
            witnessed_pct = round((float(all_high) - value) / float(all_high) * 100)

        evidence_pct = max(true_pct, witnessed_pct)

        # Does bar-clearing evidence POSTDATE the witnessed low? A stale low
        # may only stop warning when the evidence that beats it is at least
        # as fresh: expiring the fact that hurts a deal while keeping an
        # older fact that flatters it would be a thumb on the scale, not a
        # recency rule. Two independent legs, each requiring its own leg to
        # clear the threshold: the witnessed high's own date, or a window
        # whose observations all began after the low was set (the window
        # high then necessarily postdates it).
        low_ts_u = _as_utc(low_ts)
        high_ts_u = _as_utc(high_ts)
        evidence_outdates_low = low_ts_u is not None and (
            (witnessed_pct >= VERIFIED_MIN_PCT and high_ts_u is not None
             and high_ts_u >= low_ts_u)
            or (true_pct >= VERIFIED_MIN_PCT and first_ts is not None
                and _as_utc(first_ts) >= low_ts_u)
        )

        # How long the durable record has watched this item, as a CALENDAR
        # span — the honest referent for an all-time measurement. obs_days
        # counts distinct observed days, which can be 12 days spread over 5
        # months; printing it as "vs 12d" would let a 5-month verdict pose
        # as a 12-day one.
        stats_first_u = _as_utc(stats_first_ts)
        snap_ts_u = _as_utc(snap.ts)
        watched_days = (
            (snap_ts_u - stats_first_u).days
            if stats_first_u is not None and snap_ts_u is not None else None
        )

        if promo_first_ts is not None and promo_first_ts.tzinfo is None:
            promo_first_ts = promo_first_ts.replace(tzinfo=timezone.utc)

        deal = {
            "item_id": snap.item_id,
            "title": title or f"Item {snap.item_id}",
            "url": (
                f"https://www.homedepot.com{canonical_url}"
                if canonical_url
                else f"https://www.homedepot.com/s/{snap.item_id}"
            ),
            "image_url": image_url,
            "price": value,
            "original": original,
            "claimed_pct": claimed,
            "true_pct": true_pct,
            "high_window": high,
            "history_days": history_days,
            # Durable anchor from item_price_stats: a witnessed price, immune to
            # the coverage gap that makes window-based inference unreliable.
            "low_price": float(low_price) if low_price is not None else None,
            "low_ts": low_ts,
            "low_is_older": _is_older_day(low_ts, snap.ts),
            "low_is_recent": _is_recent(
                low_ts, snap.ts, settings.warn_low_recency_days),
            # Same recency dial, applied to the flattering anchor: a card
            # whose big number rests on a months-old witnessed high owes the
            # reader the recent context too (see verdict_facts).
            "high_is_recent": _is_recent(
                high_ts, snap.ts, settings.warn_low_recency_days),
            # How long today's low has been the recorded low — the dated
            # strength of "lowest recorded" (nothing lower recorded since).
            "low_age_days": (
                (snap_ts_u - low_ts_u).days
                if low_ts_u is not None and snap_ts_u is not None else None
            ),
            "evidence_outdates_low": evidence_outdates_low,
            "watched_days": watched_days,
            "price_varied": (
                low_price is not None and all_high is not None and low_price != all_high
            ),
            "obs_days": obs_days,
            "witnessed_pct": witnessed_pct,
            "evidence_pct": evidence_pct,
            "high_all": float(all_high) if all_high is not None else None,
            "special_buy": bool(snap.special_buy),
            "snapshot_ts": snap.ts,
            "promo_first_ts": promo_first_ts,
            "is_new": bool(
                promo_first_ts and (now - promo_first_ts) <= timedelta(hours=24)
            ),
            "dismissed": _is_dismissed(dismissals, ONLINE_STORE_KEY, snap.item_id, value),
            "availability_unknown": availability is None,
        }
        deal["tier"] = deal_tier(deal)
        deals.append(deal)
    return deals


async def get_online_deals(settings: Settings) -> list[dict[str, Any]]:
    """Online deals (special buys, price drops) with honest savings.

    Uses the latest snapshot per item at the reference store. claimed_pct is
    what HD advertises; true_pct compares today's price to the highest price
    we observed in the history window — the number that exposes inflated "was"
    prices. history_days reports how much history actually backs that verdict,
    so the UI can label its own confidence instead of implying a fixed span.

    Availability follows the same evidence discipline: confirmed
    out-of-stock drops off the board, items no fresh snapshot has shown to
    be buyable rank behind every deal known to be purchasable, and only a
    real fulfillment verdict counts either way.
    """
    ref_store = settings.store_list[0] if settings.store_list else None
    if ref_store is None:
        return []

    async with get_session(settings) as session:
        rows = (
            await session.execute(_online_rows_select(settings, ref_store))
        ).all()
        fulfillment_by_item = await _fulfillment_verdicts(
            session, settings, ref_store, rows
        )

    dismissals = await get_dismissals(settings)

    deals = _deals_from_rows(rows, fulfillment_by_item, dismissals, settings)
    # Board policy, applied only here: a cut nobody claims and nothing
    # measured is not worth a card, and a claim-only card whose "was" our
    # watching disproved (hollow) leaves the board entirely.
    deals = [
        d for d in deals
        if max(d["claimed_pct"], d["evidence_pct"]) >= 10 and d["tier"] != "hollow"
    ]

    # Evidence leads, HD's claim follows. Within each tier, newer deals break
    # ties first so the board rotates rather than ossifying.
    def _recency(d: dict[str, Any]):
        ts = d["promo_first_ts"] or d["snapshot_ts"]
        if ts.tzinfo is None:  # ORM timestamps are naive UTC
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    # Availability is its own evidence axis: however deep the cut, a deal
    # nothing has shown to be buyable must not outrank one we know can be
    # bought. Unknowns keep a small chipped block at the grid's tail.
    unknown = [d for d in deals if d["availability_unknown"]]
    deals = [d for d in deals if not d["availability_unknown"]]

    verified = [d for d in deals if d["tier"] == "verified"]
    unverified = [d for d in deals if d["tier"] == "unverified"]
    warned = [d for d in deals if d["tier"] == "warned"]

    verified.sort(key=_recency, reverse=True)
    verified.sort(key=lambda d: -d["evidence_pct"])  # stable: recency breaks ties
    unverified.sort(key=_recency, reverse=True)
    unverified.sort(key=lambda d: -d["claimed_pct"])
    warned.sort(key=lambda d: -d["claimed_pct"])
    unknown.sort(key=_recency, reverse=True)
    unknown.sort(key=lambda d: (-d["evidence_pct"], -d["claimed_pct"]))

    reserve = min(len(unverified), ONLINE_UNVERIFIED_SLOTS)
    keep_verified = verified[: max(0, ONLINE_DISPLAY_LIMIT - reserve)]
    keep_unverified = unverified[: max(0, ONLINE_DISPLAY_LIMIT - len(keep_verified))]
    return (
        keep_verified
        + keep_unverified
        + unknown[:ONLINE_UNKNOWN_SLOTS]
        + warned[:ONLINE_WARNING_SLOTS]
    )


async def get_daily_deal_picks(settings: Settings) -> list[dict[str, Any]]:
    """Today's Daily Deals set, every pick, with the board's honest verdicts.

    A pinned editorial strip, not a ranking: whatever the sweep matched today
    is shown in full — no depth cutoff, no slot caps, hollow included — because
    the strip's promise is "here is what HD is pushing today and what our
    record says about it". The verdict may well be unflattering ("we watched
    it sell for less"); that is the point.

    A set is current while HD's own end_date hasn't passed in Eastern time —
    daily deals roll over at 3:00 ET, so yesterday's set (end_date == today)
    stays legitimately live through the small hours. Confirmed out-of-stock
    picks are still dropped: nothing honest can be said about a price nobody
    can pay.
    """
    from zoneinfo import ZoneInfo

    ref_store = settings.store_list[0] if settings.store_list else None
    if ref_store is None:
        return []

    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    async with get_session(settings) as session:
        end_date = (
            await session.execute(
                select(func.max(DailyDealPick.end_date)).where(
                    DailyDealPick.end_date >= today_et
                )
            )
        ).scalar_one_or_none()
        if not end_date:
            return []
        item_ids = [
            r[0]
            for r in await session.execute(
                select(DailyDealPick.item_id).where(DailyDealPick.end_date == end_date)
            )
        ]
        if not item_ids:
            return []
        rows = (
            await session.execute(
                _online_rows_select(
                    settings, ref_store, item_ids=item_ids, require_promo=False
                )
            )
        ).all()
        fulfillment_by_item = await _fulfillment_verdicts(
            session, settings, ref_store, rows
        )

    dismissals = await get_dismissals(settings)
    deals = _deals_from_rows(rows, fulfillment_by_item, dismissals, settings)
    for d in deals:
        d["is_daily"] = True
        d["daily_end_date"] = end_date

    # Strongest facts first, warnings last where their chip reads as the
    # closing word; hollow ranks with unverified — its flat-price chip is the
    # honest rendering.
    rank = {"verified": 0, "warned": 2}
    deals.sort(
        key=lambda d: (rank.get(d["tier"], 1), -d["evidence_pct"], -d["claimed_pct"])
    )
    return deals
