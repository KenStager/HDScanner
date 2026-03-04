"""Schema drift detector and health checker."""

from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import Alert, AlertType, Severity
from hd.logging import get_logger

log = get_logger("pipeline.health")

# Only check paths that should be present on ALL products.
# Promotion fields (savingsCenter, percentageOff) are naturally null
# for non-clearance/non-sale items and should not trigger drift alerts.
CRITICAL_PATHS = [
    "pricing.value",
    "fulfillment.fulfillmentOptions",
    "identifiers.brandName",
    "identifiers.productLabel",
]


class HealthStatus(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


def check_drift(
    products: list[dict[str, Any]],
    threshold_pct: int = 50,
) -> tuple[HealthStatus, list[str]]:
    """Check critical JSON paths against a list of raw product dicts.

    Returns (status, list of missing paths that exceeded threshold).
    """
    if not products:
        return HealthStatus.DEGRADED, ["no products in response"]

    total = len(products)
    failed_paths: list[str] = []

    for path in CRITICAL_PATHS:
        missing = sum(1 for p in products if not _resolve_path(p, path))
        pct_missing = (missing / total) * 100
        if pct_missing > threshold_pct:
            failed_paths.append(f"{path} (missing in {pct_missing:.0f}%)")

    if failed_paths:
        return HealthStatus.DEGRADED, failed_paths

    return HealthStatus.HEALTHY, []


def _resolve_path(obj: dict | None, dotted_path: str) -> Any:
    """Navigate a dotted path like 'pricing.value' through nested dicts."""
    if obj is None:
        return None
    parts = dotted_path.split(".")
    current: Any = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


async def emit_health_degraded_alert(
    settings: Settings,
    failed_paths: list[str],
    message: str = "Schema drift detected",
) -> None:
    """Write a HEALTH_DEGRADED alert to the database with 24h dedup."""
    now = datetime.now(timezone.utc)
    async with get_session(settings) as session:
        # Check if a HEALTH_DEGRADED alert was already emitted in the last 24h
        cutoff = now - timedelta(hours=24)
        existing = await session.execute(
            select(Alert).where(
                Alert.alert_type == AlertType.HEALTH_DEGRADED,
                Alert.store_id == "SYSTEM",
                Alert.ts >= cutoff,
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            log.info("HEALTH_DEGRADED alert already exists within 24h, skipping")
            return

        session.add(Alert(
            ts=now,
            store_id="SYSTEM",
            item_id="SYSTEM",
            alert_type=AlertType.HEALTH_DEGRADED,
            severity=Severity.HIGH,
            payload={
                "message": message,
                "failed_paths": failed_paths,
            },
        ))
    log.warning("HEALTH_DEGRADED alert emitted", failed_paths=failed_paths)
