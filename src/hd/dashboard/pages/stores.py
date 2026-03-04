"""Stores page — per-store summary cards and comparison chart."""

from __future__ import annotations

from nicegui import ui

from hd.dashboard import _state
from hd.dashboard.components.charts import store_comparison_options
from hd.dashboard.components.header import render_header
from hd.dashboard.queries import get_store_summary


@ui.page("/stores")
async def stores_page() -> None:
    settings = _state.settings
    render_header(settings.dashboard_title, current_path="/stores")

    summaries = await get_store_summary(settings)

    ui.label("Store Overview").classes("text-h5 font-bold px-4")

    # Store cards
    with ui.element("div").classes("w-full px-4 grid grid-cols-1 sm:grid-cols-2 gap-4 mt-2"):
        for s in summaries:
            with ui.card().classes("p-4 w-full"):
                ui.label(f"Store {s['store_id']}").classes("text-h6 font-bold")
                if s.get("name"):
                    ui.label(s["name"]).classes("text-grey")
                ui.separator()
                with ui.column().classes("gap-1 mt-2"):
                    ui.label(f"Products tracked: {s['total_products']}")
                    with ui.row().classes("gap-4"):
                        ui.badge(f"In Stock: {s['in_stock']}").props("color=green")
                        ui.badge(f"OOS: {s['oos']}").props("color=red")
                    ui.label(f"Clearance: {s['clearance']}")
                    price_drops = s.get("price_drops_7d", 0)
                    if price_drops > 0:
                        with ui.link(target="/alerts").classes("no-underline"):
                            ui.badge(
                                f"Price Drops (7d): {price_drops}"
                            ).props("color=orange")
                    else:
                        ui.label("Price Drops (7d): 0").classes("text-grey")

    # Comparison chart
    if len(summaries) > 1:
        ui.label("Store Comparison").classes("text-h6 px-4 mt-4")
        with ui.card().classes("w-full mx-4").props("flat").style("background: transparent"):
            ui.echart(store_comparison_options(summaries)).classes("w-full h-72")
