"""Schema drift detector and health checker."""

from __future__ import annotations

import enum
from dataclasses import dataclass
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


# --- scan liveness -----------------------------------------------------------
#
# The scanner used to be unable to report its own death. A degraded run wrote
# one alert, deduped for 24 hours, and that alert was filtered out of Slack as
# "internal, not useful". On 2026-08-19 the scanner was blind from 20:00 to
# 12:00 the next day and said nothing: every run after the first logged
# "already exists within 24h, skipping". For a monitor whose normal state is
# silence, that makes the silence unfalsifiable — "nothing on sale" and "I have
# been dead since yesterday" look identical.
#
# The fix is to notify on *transitions* rather than on state. A 16-hour outage
# is two messages, stopped and resumed, which is less noise than a single deal
# group and restores the one thing that was missing: the ability to tell.

import json
from pathlib import Path


@dataclass
class ScanHealth:
    """Persisted liveness state, so a transition can be detected across runs."""

    status: HealthStatus = HealthStatus.HEALTHY
    since: str | None = None
    last_ok: str | None = None
    consecutive_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "since": self.since,
            "last_ok": self.last_ok,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScanHealth":
        raw = str(d.get("status") or HealthStatus.HEALTHY.value)
        status = (
            HealthStatus.DEGRADED
            if raw == HealthStatus.DEGRADED.value
            else HealthStatus.HEALTHY
        )
        return cls(
            status=status,
            since=d.get("since"),
            last_ok=d.get("last_ok"),
            consecutive_failures=int(d.get("consecutive_failures") or 0),
        )


def load_scan_health(path: str | Path) -> ScanHealth:
    """Read persisted state. A missing or corrupt file reads as healthy.

    Failing open matters: a damaged state file must not manufacture a phantom
    outage, and a genuine one will be re-detected on the very next run.
    """
    try:
        return ScanHealth.from_dict(json.loads(Path(path).read_text()))
    except (OSError, ValueError, TypeError):
        return ScanHealth()


def save_scan_health(path: str | Path, state: ScanHealth) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    except OSError as e:
        log.warning("Could not persist scan health state", error=str(e))


def next_scan_health(
    state: ScanHealth, ok: bool, now: datetime
) -> tuple[ScanHealth, str | None]:
    """Fold one run's outcome into the state. Returns (new_state, transition).

    transition is "degraded", "recovered", or None when the state is unchanged
    — and None is the common case, which is exactly the point: a run that
    changes nothing says nothing.
    """
    stamp = now.isoformat()
    if ok:
        recovered = state.status is HealthStatus.DEGRADED
        return (
            ScanHealth(
                status=HealthStatus.HEALTHY,
                since=stamp if recovered else (state.since or stamp),
                last_ok=stamp,
                consecutive_failures=0,
            ),
            "recovered" if recovered else None,
        )

    degraded = state.status is not HealthStatus.DEGRADED
    return (
        ScanHealth(
            status=HealthStatus.DEGRADED,
            since=stamp if degraded else state.since,
            last_ok=state.last_ok,
            consecutive_failures=state.consecutive_failures + 1,
        ),
        "degraded" if degraded else None,
    )


def outage_duration_hours(state: ScanHealth, now: datetime) -> float | None:
    """How long the scanner has been without a successful scan."""
    if not state.last_ok:
        return None
    try:
        last = datetime.fromisoformat(state.last_ok)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0.0, (now - last).total_seconds() / 3600)


async def emit_health_transition_alert(
    settings: Settings,
    transition: str,
    state: "ScanHealth",
    down_for_hours: float | None = None,
) -> None:
    """Record a liveness transition in the alerts table.

    No dedup window here: the state machine has already established that this
    is a change, and suppressing a change is what made the previous design
    unable to report a 16-hour outage.
    """
    degraded = transition == "degraded"
    async with get_session(settings) as session:
        session.add(Alert(
            ts=datetime.now(timezone.utc),
            store_id="SYSTEM",
            item_id="SYSTEM",
            alert_type=(
                AlertType.HEALTH_DEGRADED if degraded else AlertType.HEALTH_RECOVERED
            ),
            severity=Severity.HIGH if degraded else Severity.LOW,
            payload={
                "message": (
                    "Scanning stopped — a run captured no snapshots"
                    if degraded else "Scanning resumed"
                ),
                "consecutive_failures": state.consecutive_failures,
                "down_for_hours": round(down_for_hours, 1) if down_for_hours else None,
                "last_ok": state.last_ok,
            },
        ))
    log.warning(
        "Scan health transition",
        transition=transition,
        consecutive_failures=state.consecutive_failures,
        down_for_hours=round(down_for_hours, 1) if down_for_hours else None,
    )
