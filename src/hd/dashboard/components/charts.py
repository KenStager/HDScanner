"""ECharts option builders for dashboard charts."""

from __future__ import annotations

from typing import Any

from hd.dashboard.components.formatters import fmt_ts


def price_history_options(
    snapshots: list[dict],
    store_ids: list[str],
    baseline_price: float | None = None,
) -> dict[str, Any]:
    """Build ECharts options for a price-over-time line chart.

    One series per store, x-axis = time, y-axis = price.
    When baseline_price is provided, a dashed reference line is drawn.
    """
    # Group snapshots by store
    by_store: dict[str, list[dict]] = {sid: [] for sid in store_ids}
    for s in snapshots:
        sid = s.get("store_id", "")
        if sid in by_store:
            by_store[sid].append(s)

    series = []
    for sid in store_ids:
        data = [
            [fmt_ts(s["ts"]), s.get("price_value")]
            for s in by_store[sid]
            if s.get("price_value") is not None
        ]
        series.append({
            "name": f"Store {sid}",
            "type": "line",
            "smooth": True,
            "data": data,
            "connectNulls": True,
        })

    # Add baseline reference line to the first series if provided
    if baseline_price is not None and series:
        series[0]["markLine"] = {
            "silent": True,
            "symbol": "none",
            "lineStyle": {"type": "dashed", "color": "#888"},
            "data": [{
                "yAxis": baseline_price,
                "label": {
                    "formatter": f"Baseline ${baseline_price:,.2f}",
                    "position": "insideEndTop",
                },
            }],
        }

    return {
        "tooltip": {
            "trigger": "axis",
            ":valueFormatter": "value => '$' + (value ? value.toFixed(2) : '-')",
        },
        "legend": {"data": [f"Store {sid}" for sid in store_ids]},
        "xAxis": {
            "type": "time",
            "axisLabel": {
                "rotate": 30,
                "hideOverlap": True,
                "formatter": "{MM}-{dd}\n{HH}:{mm}",
            },
        },
        "yAxis": {
            "type": "value",
            "min": "dataMin",
            "axisLabel": {":formatter": "value => '$' + value.toFixed(0)"},
        },
        "series": series,
    }


def inventory_timeline_options(snapshots: list[dict], store_ids: list[str]) -> dict[str, Any]:
    """Build ECharts options for an inventory-over-time chart.

    One series per store, x-axis = time, y-axis = quantity.
    """
    by_store: dict[str, list[dict]] = {sid: [] for sid in store_ids}
    for s in snapshots:
        sid = s.get("store_id", "")
        if sid in by_store:
            by_store[sid].append(s)

    series = []
    for sid in store_ids:
        data = [
            [fmt_ts(s["ts"]), s.get("inventory_qty", 0) or 0]
            for s in by_store[sid]
        ]
        series.append({
            "name": f"Store {sid}",
            "type": "line",
            "smooth": True,
            "areaStyle": {},
            "data": data,
            "connectNulls": True,
        })

    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": [f"Store {sid}" for sid in store_ids]},
        "xAxis": {
            "type": "time",
            "axisLabel": {
                "rotate": 30,
                "hideOverlap": True,
                "formatter": "{MM}-{dd}\n{HH}:{mm}",
            },
        },
        "yAxis": {"type": "value", "name": "Qty"},
        "series": series,
    }


def store_comparison_options(summaries: list[dict]) -> dict[str, Any]:
    """Build ECharts options for a bar chart comparing stores."""
    store_labels = [f"Store {s['store_id']}" for s in summaries]

    return {
        "backgroundColor": "transparent",
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["In Stock", "Out of Stock", "Clearance"]},
        "xAxis": {"type": "category", "data": store_labels},
        "yAxis": {"type": "value"},
        "series": [
            {
                "name": "In Stock",
                "type": "bar",
                "data": [s.get("in_stock", 0) for s in summaries],
                "itemStyle": {"color": "#4caf50"},
            },
            {
                "name": "Out of Stock",
                "type": "bar",
                "data": [s.get("oos", 0) for s in summaries],
                "itemStyle": {"color": "#f44336"},
            },
            {
                "name": "Clearance",
                "type": "bar",
                "data": [s.get("clearance", 0) for s in summaries],
                "itemStyle": {"color": "#ff9800"},
            },
        ],
    }
