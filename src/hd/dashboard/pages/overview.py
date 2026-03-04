"""Overview page — dashboard home with health status, stats, and recent alerts."""

from __future__ import annotations

import asyncio

from nicegui import ui

from hd.dashboard import _state
from hd.dashboard.components.formatters import (
    alert_type_icon,
    fmt_ts,
    format_alert_details,
    severity_color,
)
from hd.dashboard.components.header import render_header
from hd.dashboard.pipeline_runner import run_pipeline_background
from hd.dashboard.queries import get_alerts, get_overview_stats


@ui.page("/")
async def overview_page() -> None:
    settings = _state.settings
    render_header(settings.dashboard_title, current_path="/")

    @ui.refreshable
    async def content() -> None:
        stats = await get_overview_stats(settings)
        recent = await get_alerts(settings, limit=10)

        # Pipeline control row
        ps = _state.pipeline_state
        with ui.row().classes("w-full items-center gap-4 px-4 mb-2"):
            run_btn = ui.button(
                "Run Pipeline",
                icon="play_arrow",
                on_click=lambda: _trigger_pipeline(settings, content),
            )
            if ps.is_running:
                run_btn.props("disable")
                ui.spinner(size="sm")
                ui.label("Running...").classes("text-orange")
            elif ps.last_run_error:
                ui.label(f"Last run failed: {ps.last_run_error}").classes("text-red text-sm")
            elif ps.last_run_result:
                r = ps.last_run_result
                ui.label(
                    f"Last run: {r['products']}p / {r['snapshots']}s / {r['alerts']}a "
                    f"at {fmt_ts(ps.last_run_ts)}"
                ).classes("text-grey text-sm")

        # Health badge + last snapshot
        with ui.row().classes("w-full items-center justify-between px-4"):
            health = stats["health_status"]
            color = "green" if health == "HEALTHY" else "red"
            with ui.row().classes("items-center gap-2"):
                ui.icon("monitor_heart", color=color).classes("text-3xl")
                ui.label(health).classes(f"text-xl font-bold text-{color}")
            ts_label = fmt_ts(stats.get("latest_snapshot_ts"))
            ui.label(f"Last snapshot: {ts_label}").classes("text-grey")

        # Stat cards — 6-column grid so they fill the full width
        with ui.element("div").classes(
            "w-full px-4 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4"
        ):
            _stat_card("inventory_2", "Active Products", stats["active_products"])
            _stat_card("camera", "Total Snapshots", stats["total_snapshots"])
            _stat_card("notifications_active", "Alerts (24h)", stats["alert_count_24h"],
                       value_color="orange" if stats["alert_count_24h"] > 0 else "")
            _stat_card("local_offer", "Clearance", stats["clearance_count"],
                       value_color="orange" if stats["clearance_count"] > 0 else "")
            _stat_card("remove_shopping_cart", "Out of Stock", stats["oos_count"],
                       value_color="red" if stats["oos_count"] > 0 else "")
            _stat_card("trending_down", "Price Drops (7d)", stats["price_drops_7d"],
                       value_color="orange" if stats["price_drops_7d"] > 0 else "")

        # Recent alerts table
        ui.label("Recent Alerts").classes("text-h6 px-4 mt-4")
        if recent:
            columns = [
                {"name": "time", "label": "Time", "field": "time", "sortable": True},
                {"name": "store", "label": "Store", "field": "store_id", "sortable": True},
                {"name": "type", "label": "Type", "field": "alert_type", "sortable": True},
                {"name": "severity", "label": "Severity", "field": "severity", "sortable": True},
                {"name": "product", "label": "Product", "field": "product_title"},
                {"name": "details", "label": "Details", "field": "details"},
            ]
            rows = [
                {
                    "time": fmt_ts(a["ts"]),
                    "store_id": a["store_id"],
                    "alert_type": a["alert_type"],
                    "severity": a["severity"],
                    "product_title": (a.get("product_title") or "")[:40],
                    "details": format_alert_details(a["alert_type"], a.get("payload")),
                }
                for a in recent
            ]
            ui.table(columns=columns, rows=rows, row_key="time").classes("w-full px-4")
        else:
            with ui.column().classes("w-full items-center py-8"):
                ui.icon("check_circle", color="green").classes("text-5xl")
                ui.label("No recent alerts").classes("text-grey text-lg mt-2")

    await content()
    ui.timer(settings.dashboard_refresh_seconds, content.refresh)


def _stat_card(icon: str, label: str, value: int | str, value_color: str = "") -> None:
    with ui.card().classes("p-4 w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.icon(icon).classes("text-2xl text-primary")
            ui.label(label).classes("text-sm text-grey")
        color_class = f"text-{value_color}" if value_color else ""
        ui.label(str(value)).classes(f"text-2xl font-bold {color_class}")


def _trigger_pipeline(settings, content_refreshable) -> None:
    """Start the pipeline in the background and refresh UI when done."""
    if _state.pipeline_state.is_running:
        ui.notification("Pipeline already running", type="warning")
        return

    async def _run_and_refresh():
        await run_pipeline_background(settings)
        ps = _state.pipeline_state
        if ps.last_run_error:
            ui.notification(f"Pipeline failed: {ps.last_run_error}", type="negative")
        else:
            r = ps.last_run_result or {}
            ui.notification(
                f"Pipeline complete: {r.get('products', 0)}p / {r.get('snapshots', 0)}s / {r.get('alerts', 0)}a",
                type="positive",
            )
        content_refreshable.refresh()

    ui.notification("Pipeline started", type="info")
    asyncio.create_task(_run_and_refresh())
