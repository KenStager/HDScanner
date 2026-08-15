"""Alert writer — persists Alert objects to the database."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, func, and_

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import Alert
from hd.logging import get_logger

log = get_logger("pipeline.alerts")


def _state_fingerprint(alert: Alert) -> tuple:
    """Extract the material state fields from an alert's payload.

    Two alerts with identical fingerprints represent the same deal at the same
    price/stock — re-alerting adds no new information.

    Fields considered:
      - price_value (online price)
      - clearance_value (in-store clearance price)
      - in_stock (boolean stock status)
      - inventory_qty (unit count)
      - pct_drop (for PRICE_DROP alerts)
    """
    p = alert.payload or {}
    after = p.get("after", {})
    return (
        after.get("price_value"),
        after.get("clearance_value"),
        after.get("in_stock"),
        after.get("inventory_qty"),
        p.get("clearance_value"),
        p.get("pct_drop"),
    )


async def write_alerts(settings: Settings, alerts: list[Alert]) -> int:
    """Bulk insert alerts into the database with content-based deduplication.

    For each candidate alert, compares against the most recent existing alert
    with the same (store_id, item_id, alert_type).  If the material state
    (price, clearance, stock) is unchanged, the alert is suppressed regardless
    of how much time has passed.  This prevents repeated notifications for
    deals whose state hasn't changed between scan cycles.

    Returns the number of alerts written.
    """
    if not alerts:
        return 0

    async with get_session(settings) as session:
        # Batch-load the most recent alert per (store_id, item_id, alert_type)
        # for all candidate keys in a single query.
        max_ts_sub = (
            select(
                Alert.store_id,
                Alert.item_id,
                Alert.alert_type,
                func.max(Alert.ts).label("max_ts"),
            )
            .group_by(Alert.store_id, Alert.item_id, Alert.alert_type)
            .subquery()
        )
        result = await session.execute(
            select(Alert).join(
                max_ts_sub,
                and_(
                    Alert.store_id == max_ts_sub.c.store_id,
                    Alert.item_id == max_ts_sub.c.item_id,
                    Alert.alert_type == max_ts_sub.c.alert_type,
                    Alert.ts == max_ts_sub.c.max_ts,
                ),
            )
        )
        last_by_key: dict[tuple, Alert] = {
            (a.store_id, a.item_id, a.alert_type): a
            for a in result.scalars().all()
        }

        written = 0
        for alert in alerts:
            key = (alert.store_id, alert.item_id, alert.alert_type)
            last = last_by_key.get(key)

            if last is not None and _state_fingerprint(last) == _state_fingerprint(alert):
                continue  # Same state as last alert — suppress

            session.add(alert)
            last_by_key[key] = alert  # Update for within-batch dedup
            written += 1

    log.info("Alerts written", count=written, skipped=len(alerts) - written)
    return written
