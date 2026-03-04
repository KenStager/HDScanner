"""Alert writer — persists Alert objects to the database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import Alert
from hd.logging import get_logger

log = get_logger("pipeline.alerts")


async def write_alerts(settings: Settings, alerts: list[Alert]) -> int:
    """Bulk insert alerts into the database with 24-hour deduplication.

    Returns the number of alerts written.
    """
    if not alerts:
        return 0

    async with get_session(settings) as session:
        # Load recent alerts for dedup (last 24 hours)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = await session.execute(
            select(Alert.store_id, Alert.item_id, Alert.alert_type)
            .where(Alert.ts >= cutoff)
        )
        existing = {(r.store_id, r.item_id, r.alert_type) for r in recent.all()}

        written = 0
        for alert in alerts:
            key = (alert.store_id, alert.item_id, alert.alert_type)
            if key not in existing:
                session.add(alert)
                existing.add(key)  # Prevent dupes within same batch
                written += 1

    log.info("Alerts written", count=written, skipped=len(alerts) - written)
    return written
