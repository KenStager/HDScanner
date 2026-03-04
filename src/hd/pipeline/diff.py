"""Diff engine — compares consecutive snapshots and generates alerts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, desc

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import StoreSnapshot, Alert, AlertType, Severity, Product
from hd.logging import get_logger

log = get_logger("pipeline.diff")


async def run_diff(settings: Settings) -> list[Alert]:
    """Compare latest vs previous snapshots for all (store, item) pairs.

    Returns a list of Alert ORM objects (not yet persisted).
    """
    alerts: list[Alert] = []

    async with get_session(settings) as session:
        # Get all distinct (store_id, item_id) pairs
        pairs_result = await session.execute(
            select(StoreSnapshot.store_id, StoreSnapshot.item_id)
            .distinct()
        )
        pairs = pairs_result.all()

        # Pre-load all products for O(1) lookup (M6 fix)
        prod_result = await session.execute(select(Product))
        product_map = {p.item_id: p for p in prod_result.scalars().all()}

        for store_id, item_id in pairs:

            # Fetch the two most recent snapshots
            result = await session.execute(
                select(StoreSnapshot)
                .where(
                    StoreSnapshot.store_id == store_id,
                    StoreSnapshot.item_id == item_id,
                )
                .order_by(desc(StoreSnapshot.ts))
                .limit(2)
            )
            snapshots = result.scalars().all()

            if len(snapshots) < 2:
                continue  # First snapshot — no diff possible

            curr = snapshots[0]
            prev = snapshots[1]

            gap_hours = (curr.ts - prev.ts).total_seconds() / 3600

            # Stale gap: skip entirely
            if gap_hours > settings.diff_stale_gap_hours:
                log.warning(
                    "Stale gap, skipping diff",
                    store_id=store_id,
                    item_id=item_id,
                    gap_hours=round(gap_hours, 1),
                )
                continue

            product = product_map.get(item_id)
            pair_alerts = _diff_snapshots(prev, curr, product)

            # Moderate gap: annotate alerts
            if gap_hours > settings.diff_gap_threshold_hours:
                for alert in pair_alerts:
                    alert.payload = {
                        **(alert.payload or {}),
                        "gap_warning": True,
                        "gap_hours": round(gap_hours, 1),
                    }

            alerts.extend(pair_alerts)

    log.info("Diff complete", pairs=len(pairs), alerts=len(alerts))
    return alerts


def _diff_snapshots(
    prev: StoreSnapshot,
    curr: StoreSnapshot,
    product: Product | None,
) -> list[Alert]:
    """Apply diff rules between two snapshots. Return Alert objects."""
    alerts: list[Alert] = []
    now = datetime.now(timezone.utc)
    base_payload = _build_base_payload(prev, curr, product)

    # PRICE_DROP — only deep discounts (>25%)
    if (
        curr.price_value is not None
        and prev.price_value is not None
        and curr.price_value < prev.price_value
    ):
        pct_drop = float((prev.price_value - curr.price_value) / prev.price_value * 100)
        if pct_drop > 25:
            severity = Severity.HIGH if pct_drop > 50 else Severity.MEDIUM
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.PRICE_DROP,
                severity=severity,
                payload={**base_payload, "pct_drop": round(pct_drop, 1)},
            ))

    # CLEARANCE
    if (
        curr.savings_center == "CLEARANCE"
        and prev.savings_center != "CLEARANCE"
    ):
        pct_off = curr.percentage_off or 0
        severity = Severity.HIGH if pct_off >= 50 else Severity.MEDIUM

        alerts.append(Alert(
            ts=now,
            store_id=curr.store_id,
            item_id=curr.item_id,
            alert_type=AlertType.CLEARANCE,
            severity=severity,
            payload=base_payload,
        ))

    return alerts


def _build_base_payload(
    prev: StoreSnapshot,
    curr: StoreSnapshot,
    product: Product | None,
) -> dict[str, Any]:
    """Build the common payload with before/after values."""
    return {
        "before": _snapshot_to_dict(prev),
        "after": _snapshot_to_dict(curr),
        "product_title": product.title if product else None,
        "product_url": (
            f"https://www.homedepot.com{product.canonical_url}"
            if product and product.canonical_url
            else None
        ),
    }


def _snapshot_to_dict(snap: StoreSnapshot) -> dict[str, Any]:
    return {
        "price_value": float(snap.price_value) if snap.price_value is not None else None,
        "price_original": float(snap.price_original) if snap.price_original is not None else None,
        "savings_center": snap.savings_center,
        "percentage_off": snap.percentage_off,
        "special_buy": snap.special_buy,
        "in_stock": snap.in_stock,
        "inventory_qty": snap.inventory_qty,
    }
