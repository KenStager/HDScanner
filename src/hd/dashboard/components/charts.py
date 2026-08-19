"""ECharts option builders for dashboard charts.

Color follows the entity, never its rank on the page. Orange is the online
price — the same association the deal board's online flash carries — and the
stores take blue and pink in config order. The trio was validated for the
dark surface (#242629): lightness band, all-pairs CVD separation ΔE ≥ 10.7,
contrast ≥ 3:1. Measure is carried by line style, not hue: prices are solid,
in-store clearance is a dashed step line in its store's hue.

price_value is the site price in a store's context, and the two stores agree
on it for ~97% of items. When they agree wherever they overlap, the chart
draws one "Online price" series instead of the same line twice; the rare
store-localized items keep honest per-store lines.
"""

from __future__ import annotations

from typing import Any

from hd.dashboard.components.formatters import fmt_ts

# The online price hue — matches the deal board's online-deal flash
ONLINE_SERIES_COLOR = "#E85C02"
# Store hues, assigned by position in the configured store list
STORE_SERIES_COLORS = ["#3D8BE8", "#C75FA8"]

_TEXT = "#ECEDEE"
_MUTED = "#9BA0A6"
_GRID_LINE = "rgba(255,255,255,0.07)"
_TOOLTIP_BG = "#2E3033"

_AXIS_COMMON: dict[str, Any] = {
    "axisLine": {"lineStyle": {"color": _GRID_LINE}},
    "axisTick": {"show": False},
}


def _base_options(store_labels: list[str]) -> dict[str, Any]:
    """Shared dark-theme scaffolding for time-series charts."""
    return {
        "backgroundColor": "transparent",
        "textStyle": {"color": _MUTED, "fontFamily": "Inter, sans-serif"},
        "legend": {
            "data": store_labels,
            "textStyle": {"color": _TEXT},
            "icon": "roundRect",
            "itemWidth": 14,
            "itemHeight": 4,
            "top": 0,
            # One scrollable row — a wrapped legend collides with the plot on
            # narrow screens
            "type": "scroll",
        },
        "grid": {"left": 48, "right": 24, "top": 36, "bottom": 42},
        "tooltip": {
            "trigger": "axis",
            "backgroundColor": _TOOLTIP_BG,
            "borderColor": _GRID_LINE,
            "textStyle": {"color": _TEXT},
        },
        "xAxis": {
            "type": "time",
            **_AXIS_COMMON,
            "axisLabel": {
                "color": _MUTED,
                "hideOverlap": True,
                # Leveled: hour ticks show the time, day boundaries the date —
                # otherwise a short window repeats the same date across the axis
                "formatter": {
                    "year": "{yyyy}",
                    "month": "{MMM}",
                    "day": "{MMM} {d}",
                    "hour": "{HH}:{mm}",
                    "minute": "{HH}:{mm}",
                },
            },
            "splitLine": {"show": False},
        },
        "yAxis": {
            "type": "value",
            **_AXIS_COMMON,
            "axisLabel": {"color": _MUTED},
            "splitLine": {"lineStyle": {"color": _GRID_LINE}},
        },
    }


def online_prices_agree(snapshots: list[dict], store_ids: list[str]) -> bool:
    """Do the stores tell the same price story wherever their scans overlap?

    Scans of the two stores land minutes apart, so exact timestamps never
    match; readings are bucketed by hour and the sets of prices seen in each
    shared hour are compared. A single-store item trivially agrees. Any
    disagreement means the item is store-localized and must keep per-store
    series — collapsing them would average away a real difference.
    """
    hourly: dict[str, dict[str, set[float]]] = {}
    for s in snapshots:
        sid = s.get("store_id", "")
        price = s.get("price_value")
        if sid not in store_ids or price is None:
            continue
        bucket = fmt_ts(s["ts"])[:13]
        hourly.setdefault(sid, {}).setdefault(bucket, set()).add(float(price))
    maps = list(hourly.values())
    if len(maps) < 2:
        return True
    base = maps[0]
    for other in maps[1:]:
        for hour in base.keys() & other.keys():
            if base[hour] != other[hour]:
                return False
    return True


def price_history_options(
    snapshots: list[dict],
    store_ids: list[str],
    store_names: dict[str, str] | None = None,
    low_anchor: tuple[float, str] | None = None,
) -> dict[str, Any]:
    """Price-over-time options, plus a dashed step series per store while an
    in-store clearance price is present.

    The site price draws as one "Online price" series when the stores agree
    on it (see online_prices_agree); store-localized items fall back to a
    solid line per store.

    low_anchor is (price, label) for the witnessed low from item_price_stats —
    a dated, gap-immune fact — drawn as a recessive reference line. The caller
    passes it only when it sits below what the window already shows.
    """
    names = store_names or {}
    by_store: dict[str, list[dict]] = {sid: [] for sid in store_ids}
    for s in snapshots:
        sid = s.get("store_id", "")
        if sid in by_store:
            by_store[sid].append(s)

    merged = online_prices_agree(snapshots, store_ids)

    series: list[dict[str, Any]] = []
    labels: list[str] = []

    if merged:
        # snapshots arrive ASC, so the cross-store union stays chronological
        online_data = [
            [fmt_ts(s["ts"]), s.get("price_value")]
            for s in snapshots
            if s.get("store_id") in by_store and s.get("price_value") is not None
        ]
        if online_data:
            labels.append("Online price")
            series.append({
                "name": "Online price",
                "type": "line",
                "data": online_data,
                "connectNulls": True,
                # A 1-2 point series draws no visible line — show the points
                "showSymbol": len(online_data) <= 2,
                "symbolSize": 7,
                "lineStyle": {"width": 2, "color": ONLINE_SERIES_COLOR},
                "itemStyle": {"color": ONLINE_SERIES_COLOR},
                "emphasis": {"disabled": True},
            })

    for i, sid in enumerate(store_ids):
        name = names.get(sid) or f"Store {sid}"
        color = STORE_SERIES_COLORS[i % len(STORE_SERIES_COLORS)]
        if not merged:
            price_data = [
                [fmt_ts(s["ts"]), s.get("price_value")]
                for s in by_store[sid]
                if s.get("price_value") is not None
            ]
            if price_data:
                labels.append(name)
                series.append({
                    "name": name,
                    "type": "line",
                    "data": price_data,
                    "connectNulls": True,
                    "showSymbol": len(price_data) <= 2,
                    "symbolSize": 7,
                    "lineStyle": {"width": 2, "color": color},
                    "itemStyle": {"color": color},
                    "emphasis": {"disabled": True},
                })
        clearance_data = [
            [fmt_ts(s["ts"]), s.get("clearance_value")]
            for s in by_store[sid]
            if s.get("clearance_value") is not None
        ]
        if clearance_data:
            cname = f"{name} clearance"
            labels.append(cname)
            series.append({
                "name": cname,
                "type": "line",
                "step": "end",
                "data": clearance_data,
                "connectNulls": True,
                "showSymbol": len(clearance_data) <= 2,
                "symbolSize": 7,
                "lineStyle": {"width": 2, "color": color, "type": "dashed"},
                "itemStyle": {"color": color},
                "emphasis": {"disabled": True},
            })

    if low_anchor is not None and series:
        price, label = low_anchor
        series[0]["markLine"] = {
            "silent": True,
            "symbol": "none",
            "lineStyle": {"type": "dotted", "color": _MUTED},
            "label": {
                "formatter": label,
                "position": "insideEndTop",
                "color": _MUTED,
            },
            "data": [{"yAxis": price}],
        }

    options = _base_options(labels)
    options["tooltip"][":valueFormatter"] = "value => '$' + (value ? value.toFixed(2) : '-')"
    options["yAxis"]["min"] = "dataMin"
    options["yAxis"]["axisLabel"][":formatter"] = "value => '$' + value.toFixed(0)"
    options["series"] = series
    return options


def inventory_timeline_options(
    snapshots: list[dict],
    store_ids: list[str],
    store_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Inventory-over-time options, one step series per store.

    Steps, not smooth curves: quantity is a count read at scan time, and a
    curve would invent movement between observations.
    """
    names = store_names or {}
    by_store: dict[str, list[dict]] = {sid: [] for sid in store_ids}
    for s in snapshots:
        sid = s.get("store_id", "")
        if sid in by_store:
            by_store[sid].append(s)

    series: list[dict[str, Any]] = []
    labels: list[str] = []
    for i, sid in enumerate(store_ids):
        data = [
            [fmt_ts(s["ts"]), s.get("inventory_qty", 0) or 0]
            for s in by_store[sid]
        ]
        if not data:
            continue
        name = names.get(sid) or f"Store {sid}"
        color = STORE_SERIES_COLORS[i % len(STORE_SERIES_COLORS)]
        labels.append(name)
        series.append({
            "name": name,
            "type": "line",
            "step": "end",
            "data": data,
            "connectNulls": True,
            "showSymbol": len(data) <= 2,
            "symbolSize": 7,
            "lineStyle": {"width": 2, "color": color},
            "itemStyle": {"color": color},
            "areaStyle": {"color": color, "opacity": 0.10},
            "emphasis": {"disabled": True},
        })

    options = _base_options(labels)
    options["yAxis"]["name"] = "units"
    options["yAxis"]["nameTextStyle"] = {"color": _MUTED}
    options["series"] = series
    return options


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
