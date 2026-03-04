"""Background pipeline runner for the dashboard."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from hd.config import Settings
from hd.dashboard._state import pipeline_state
from hd.logging import get_logger
from hd.pipeline.alerts import write_alerts
from hd.pipeline.diff import run_diff
from hd.pipeline.discovery import run_discovery
from hd.pipeline.snapshot import run_snapshots

log = get_logger("dashboard.pipeline_runner")


async def run_pipeline_background(settings: Settings) -> None:
    """Run the full pipeline in the background (non-blocking for NiceGUI).

    Prevents concurrent runs via lock. Updates pipeline_state throughout.
    """
    if pipeline_state._lock.locked():
        log.info("Pipeline already running, skipping")
        return

    async with pipeline_state._lock:
        pipeline_state.is_running = True
        pipeline_state.last_run_error = None

        try:
            product_count = await run_discovery(
                settings=settings,
                brands=settings.brand_list,
                max_pages=settings.max_pages,
            )
            log.info("Dashboard pipeline: discovery complete", products=product_count)

            if settings.stage_delay_seconds > 0:
                await asyncio.sleep(settings.stage_delay_seconds)

            snapshot_count = await run_snapshots(
                settings=settings,
                store_ids=settings.store_list,
            )
            log.info("Dashboard pipeline: snapshots complete", rows=snapshot_count)

            alerts_list = await run_diff(settings=settings)
            alert_count = 0
            if alerts_list:
                alert_count = await write_alerts(settings=settings, alerts=alerts_list)
            log.info("Dashboard pipeline: diff complete", alerts=alert_count)

            pipeline_state.last_run_result = {
                "products": product_count,
                "snapshots": snapshot_count,
                "alerts": alert_count,
            }
            pipeline_state.last_run_ts = datetime.now(timezone.utc)

        except Exception as e:
            log.error("Dashboard pipeline failed", error=str(e))
            pipeline_state.last_run_error = str(e)
            pipeline_state.last_run_ts = datetime.now(timezone.utc)

        finally:
            pipeline_state.is_running = False
