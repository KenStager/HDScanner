"""Async DB query functions for the dashboard data layer.

All functions return dicts (not ORM objects) and accept Settings
to obtain a DB session via the existing base.py pattern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import Alert, AlertType, Product, Severity, Store, StoreSnapshot


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

        # Clearance count: latest snapshots with savings_center == 'CLEARANCE'
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
                .where(StoreSnapshot.savings_center == "CLEARANCE")
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

        # Health status: check for recent HEALTH_DEGRADED alert
        degraded = (
            await session.execute(
                select(Alert)
                .where(Alert.alert_type == AlertType.HEALTH_DEGRADED)
                .order_by(desc(Alert.ts))
                .limit(1)
            )
        ).scalar_one_or_none()

        health_status = "DEGRADED" if degraded else "HEALTHY"

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
            return {"product": None, "snapshots": [], "alerts": []}

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

        return {
            "product": {
                "item_id": product.item_id,
                "brand": product.brand,
                "title": product.title,
                "model_number": product.model_number,
                "canonical_url": product.canonical_url,
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
            select(Alert, Product.title.label("product_title"))
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

        return [
            {
                "id": row.Alert.id,
                "ts": row.Alert.ts,
                "store_id": row.Alert.store_id,
                "item_id": row.Alert.item_id,
                "alert_type": row.Alert.alert_type.value,
                "severity": row.Alert.severity.value,
                "payload": row.Alert.payload,
                "product_title": row.product_title,
            }
            for row in rows
        ]


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
