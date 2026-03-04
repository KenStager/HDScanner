"""Alerts page — filterable alert feed with expandable detail rows."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from nicegui import ui

from hd.dashboard import _state
from hd.dashboard.components.formatters import (
    alert_type_icon,
    fmt_pct_nonzero,
    fmt_price,
    fmt_savings_center,
    fmt_ts,
    fmt_ts_relative,
    format_price_change,
    severity_color,
    stock_badge,
)
from hd.dashboard.components.header import render_header
from hd.dashboard.queries import get_alerts
from hd.db.models import AlertType, Severity

# ---------------------------------------------------------------------------
# Alert grouping helpers (pure logic, no NiceGUI dependency)
# ---------------------------------------------------------------------------

_SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
_GROUP_WINDOW_MINUTES: int = 10


def _parse_ts(ts: datetime | str | None) -> datetime:
    """Coerce a timestamp to an aware datetime, default to epoch on None."""
    if ts is None:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    if isinstance(ts, str):
        dt = datetime.fromisoformat(ts)
    else:
        dt = ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _group_alerts(alerts_list: list[dict]) -> list[dict]:
    """Group alerts by (item_id, alert_type) within a 10-minute window.

    Returns a list of *group* dicts (one per group), sorted most-recent first.
    """
    # Bucket by (item_id, alert_type)
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for a in alerts_list:
        key = (str(a.get("item_id", "")), str(a.get("alert_type", "")))
        buckets[key].append(a)

    groups: list[dict] = []
    window = timedelta(minutes=_GROUP_WINDOW_MINUTES)

    for _key, bucket in buckets.items():
        # Sort bucket by timestamp ascending for sub-grouping
        bucket.sort(key=lambda a: _parse_ts(a.get("ts")))

        sub_group: list[dict] = [bucket[0]]
        for a in bucket[1:]:
            prev_ts = _parse_ts(sub_group[-1].get("ts"))
            cur_ts = _parse_ts(a.get("ts"))
            if (cur_ts - prev_ts) <= window:
                sub_group.append(a)
            else:
                groups.append(_build_group(sub_group))
                sub_group = [a]
        groups.append(_build_group(sub_group))

    # Sort groups by most recent timestamp descending
    groups.sort(key=lambda g: g["ts_dt"], reverse=True)
    return groups


def _build_group(store_alerts: list[dict]) -> dict:
    """Build a representative group dict from one or more per-store alerts."""
    # Pick representative: highest severity, then largest pct_drop
    def _rank(a: dict) -> tuple[int, float]:
        sev = _SEVERITY_RANK.get(str(a.get("severity", "")).lower(), -1)
        pct = float((a.get("payload") or {}).get("pct_drop") or 0)
        return (sev, pct)

    rep = max(store_alerts, key=_rank)
    most_recent = max(store_alerts, key=lambda a: _parse_ts(a.get("ts")))
    earliest = min(store_alerts, key=lambda a: a.get("id", 0))

    store_ids = sorted({str(a.get("store_id", "")) for a in store_alerts})
    store_ids_display = ", ".join(store_ids)

    item_id = rep.get("item_id", "")
    alert_type = rep.get("alert_type", "")
    earliest_id = earliest.get("id", 0)
    group_key = f"{item_id}_{alert_type}_{earliest_id}"

    return {
        "group_key": group_key,
        "store_count": len(store_ids),
        "store_ids_display": store_ids_display,
        "ts": most_recent.get("ts"),
        "ts_dt": _parse_ts(most_recent.get("ts")),
        "item_id": item_id,
        "alert_type": alert_type,
        "severity": rep.get("severity", ""),
        "payload": rep.get("payload") or {},
        "product_title": rep.get("product_title") or "",
        "store_alerts": store_alerts,
    }


@ui.page("/alerts")
async def alerts_page() -> None:
    settings = _state.settings
    store_ids = settings.store_list
    render_header(settings.dashboard_title, current_path="/alerts")

    ui.add_css("""
        .q-table tbody tr.main-row:hover {
            background: rgba(255,255,255,0.05) !important;
            cursor: pointer;
        }
    """)

    # Filter options
    type_options = ["All"] + [t.value for t in AlertType]
    severity_options = ["All"] + [s.value for s in Severity]
    store_options = ["All"] + store_ids

    # Filter bar — horizontal row that wraps on small screens
    with ui.row().classes("w-full items-end gap-4 px-4 flex-wrap"):
        type_select = ui.select(type_options, value="All", label="Type").classes("w-40")
        severity_select = ui.select(severity_options, value="All", label="Severity").classes("w-40")
        store_select = ui.select(store_options, value="All", label="Store").classes("w-40")
        since_input = ui.number("Since (hours)", value=None, min=1, max=720).classes("w-32")
        limit_input = ui.number("Limit", value=50, min=1, max=500).classes("w-24")
        # Button placeholder — on_click wired after alert_table is defined
        apply_btn = ui.button("Apply Filters").props("color=primary")

        def reset_filters():
            type_select.value = "All"
            severity_select.value = "All"
            store_select.value = "All"
            since_input.value = None
            limit_input.value = 50
            alert_table.refresh()

        ui.button("Reset", icon="refresh").props("flat color=grey").on_click(reset_filters)

    @ui.refreshable
    async def alert_table() -> None:
        alert_type = type_select.value if type_select.value != "All" else None
        severity = severity_select.value if severity_select.value != "All" else None
        store_id = store_select.value if store_select.value != "All" else None
        since = int(since_input.value) if since_input.value else None
        limit = int(limit_input.value) if limit_input.value else 50

        alerts_list = await get_alerts(
            settings,
            alert_type=alert_type,
            severity=severity,
            store_id=store_id,
            since_hours=since,
            limit=limit,
        )

        if not alerts_list:
            with ui.column().classes("w-full items-center py-8"):
                ui.icon("filter_list_off").classes("text-5xl text-grey")
                ui.label("No alerts match the current filters.").classes("text-grey text-lg mt-2")
            return

        columns = [
            {"name": "expand", "label": "", "field": "expand", "sortable": False,
             "style": "width: 40px"},
            {"name": "time", "label": "Time", "field": "time", "sortable": True,
             "sortField": "time_sort"},
            {"name": "store", "label": "Store(s)", "field": "store_id", "sortable": True},
            {"name": "item", "label": "Item", "field": "item_id", "sortable": True},
            {"name": "type", "label": "Type", "field": "alert_type", "sortable": True},
            {"name": "severity", "label": "Severity", "field": "severity", "sortable": True},
            {"name": "product", "label": "Product", "field": "product_title"},
            {"name": "details", "label": "Details", "field": "details"},
        ]

        grouped = _group_alerts(alerts_list)

        rows = []
        for g in grouped:
            payload = g["payload"]
            # Build per-store detail entries
            store_details = []
            for sa in sorted(g["store_alerts"], key=lambda x: str(x.get("store_id", ""))):
                sp = sa.get("payload") or {}
                sb = sp.get("before", {})
                saf = sp.get("after", {})
                sb_stock_label, sb_stock_color = stock_badge(sb.get("in_stock"))
                sa_stock_label, sa_stock_color = stock_badge(saf.get("in_stock"))
                store_details.append({
                    "store_id": sa.get("store_id", ""),
                    "severity": sa.get("severity", ""),
                    "severity_color": severity_color(sa.get("severity", "")),
                    "time_abs": fmt_ts(sa.get("ts")),
                    "time_rel": fmt_ts_relative(sa.get("ts")),
                    "gap_warning": sp.get("gap_warning", False),
                    "gap_hours": sp.get("gap_hours"),
                    "before_price": fmt_price(sb.get("price_value")),
                    "after_price": fmt_price(saf.get("price_value")),
                    "pct_drop": sp.get("pct_drop"),
                    "after_pct_off": fmt_pct_nonzero(saf.get("percentage_off")),
                    "before_stock_label": sb_stock_label,
                    "before_stock_color": sb_stock_color,
                    "before_qty": sb.get("inventory_qty"),
                    "before_savings_center": fmt_savings_center(sb.get("savings_center")),
                    "before_pct_off": fmt_pct_nonzero(sb.get("percentage_off")),
                    "after_stock_label": sa_stock_label,
                    "after_stock_color": sa_stock_color,
                    "after_qty": saf.get("inventory_qty"),
                    "after_savings_center": fmt_savings_center(saf.get("savings_center")),
                })

            rows.append({
                "id": g["group_key"],
                "time": fmt_ts_relative(g["ts"]),
                "time_abs": fmt_ts(g["ts"]),
                "time_sort": g["ts"].isoformat() if hasattr(g["ts"], "isoformat") else "",
                "store_id": g["store_ids_display"],
                "store_count": g["store_count"],
                "item_id": g["item_id"],
                "alert_type": g["alert_type"],
                "severity": g["severity"],
                "severity_color": severity_color(g["severity"]),
                "type_icon": alert_type_icon(g["alert_type"]),
                "product_title": g["product_title"][:40],
                "product_title_full": g["product_title"],
                "details": format_price_change(g["alert_type"], payload),
                "product_url": payload.get("product_url"),
                "store_details": store_details,
            })

        with ui.element("div").classes("w-full overflow-x-auto px-4"):
            table = ui.table(
                columns=columns,
                rows=rows,
                row_key="id",
                pagination=25,
            ).classes("w-full")

            table.add_slot("body", r"""
                <q-tr :props="props" @click="props.expand = !props.expand"
                       class="main-row">
                    <q-td auto-width>
                        <q-icon :name="props.expand ? 'expand_less' : 'expand_more'"
                                size="sm" color="grey" />
                    </q-td>
                    <q-td key="time" :props="props">
                        <span>{{ props.row.time }}</span>
                        <q-tooltip>{{ props.row.time_abs }}</q-tooltip>
                    </q-td>
                    <q-td key="store" :props="props">
                        <span>{{ props.row.store_id }}</span>
                        <q-badge v-if="props.row.store_count > 1"
                                 color="blue-grey" class="q-ml-xs"
                                 :label="props.row.store_count + ' stores'" />
                    </q-td>
                    <q-td key="item" :props="props">
                        <div class="row items-center no-wrap q-gutter-xs">
                            <span>{{ props.row.item_id }}</span>
                            <a :href="'/products/' + props.row.item_id"
                               class="q-btn q-btn--flat q-btn--round q-btn--dense q-btn--actionable"
                               style="min-height: 24px; min-width: 24px; padding: 2px;"
                               @click.stop>
                                <q-icon name="open_in_new" size="xs" />
                            </a>
                        </div>
                    </q-td>
                    <q-td key="type" :props="props">
                        <div class="row items-center no-wrap q-gutter-xs">
                            <q-icon :name="props.row.type_icon" size="xs" />
                            <span>{{ props.row.alert_type }}</span>
                        </div>
                    </q-td>
                    <q-td key="severity" :props="props">
                        <q-badge :color="props.row.severity_color"
                                 :label="props.row.severity" />
                    </q-td>
                    <q-td key="product" :props="props">{{ props.row.product_title }}</q-td>
                    <q-td key="details" :props="props">
                        <span :style="props.row.alert_type === 'PRICE_DROP'
                            ? 'color: orange; font-weight: bold' : ''">
                            {{ props.row.details }}
                        </span>
                    </q-td>
                </q-tr>

                <q-tr v-show="props.expand" :props="props">
                    <q-td colspan="100%">
                        <div class="q-pa-md" style="background: rgba(255,255,255,0.02);">

                            <div v-if="props.row.product_title_full"
                                 class="text-subtitle1"
                                 style="font-weight: bold;">
                                {{ props.row.product_title_full }}
                            </div>

                            <div class="text-caption text-grey q-mb-sm">
                                Item {{ props.row.item_id }}
                            </div>

                            <!-- Per-store sections -->
                            <template v-for="(sd, idx) in props.row.store_details"
                                      :key="sd.store_id">

                                <q-separator v-if="idx > 0"
                                             class="q-my-md"
                                             style="opacity: 0.3;" />

                                <div class="row items-center q-gutter-xs q-mb-xs">
                                    <q-icon name="store" size="xs" color="grey" />
                                    <span class="text-subtitle2">
                                        Store {{ sd.store_id }}
                                    </span>
                                    <q-badge :color="sd.severity_color"
                                             :label="sd.severity" />
                                    <span class="text-caption text-grey">
                                        {{ sd.time_abs }}
                                    </span>
                                </div>

                                <div v-if="sd.gap_warning" class="q-mb-sm">
                                    <q-banner dense class="bg-warning text-dark rounded-borders">
                                        <template v-slot:avatar>
                                            <q-icon name="schedule" />
                                        </template>
                                        Data gap: {{ sd.gap_hours }} hours between snapshots.
                                        Changes may not be precise.
                                    </q-banner>
                                </div>

                                <div class="row items-center q-gutter-sm q-mb-sm">
                                    <span style="text-decoration: line-through; opacity: 0.6;"
                                          class="text-h6">
                                        {{ sd.before_price }}
                                    </span>
                                    <q-icon name="arrow_forward" />
                                    <span class="text-h6 text-orange"
                                          style="font-weight: bold;">
                                        {{ sd.after_price }}
                                    </span>
                                    <q-badge v-if="sd.pct_drop" color="orange"
                                             :label="sd.pct_drop + '% off'" />
                                    <q-badge v-else-if="sd.after_pct_off !== '-'" color="orange"
                                             :label="sd.after_pct_off + ' off'" />
                                </div>

                                <div class="row q-col-gutter-md q-mb-sm">
                                    <div class="col-12 col-sm-6">
                                        <div class="text-subtitle2 text-grey q-mb-xs">Before</div>
                                        <div class="q-gutter-xs">
                                            <div>
                                                <strong>Stock:</strong>
                                                <q-badge :color="sd.before_stock_color"
                                                         :label="sd.before_stock_label" />
                                                <span v-if="sd.before_qty != null"
                                                      class="q-ml-xs text-grey">
                                                    ({{ sd.before_qty }} units)
                                                </span>
                                            </div>
                                            <div><strong>Promo:</strong> {{ sd.before_savings_center }}</div>
                                            <div><strong>Discount:</strong> {{ sd.before_pct_off }}</div>
                                        </div>
                                    </div>
                                    <div class="col-12 col-sm-6">
                                        <div class="text-subtitle2 text-grey q-mb-xs">After</div>
                                        <div class="q-gutter-xs">
                                            <div>
                                                <strong>Stock:</strong>
                                                <q-badge :color="sd.after_stock_color"
                                                         :label="sd.after_stock_label" />
                                                <span v-if="sd.after_qty != null"
                                                      class="q-ml-xs text-grey">
                                                    ({{ sd.after_qty }} units)
                                                </span>
                                            </div>
                                            <div><strong>Promo:</strong> {{ sd.after_savings_center }}</div>
                                            <div><strong>Discount:</strong> {{ sd.after_pct_off }}</div>
                                        </div>
                                    </div>
                                </div>

                            </template>

                            <div class="q-mt-sm row q-gutter-sm">
                                <a :href="'/products/' + props.row.item_id"
                                   class="q-btn q-btn--flat q-btn--dense q-btn--actionable"
                                   style="text-decoration: none; color: inherit; font-size: 0.85rem; padding: 4px 8px;"
                                   @click.stop>
                                    <q-icon name="visibility" size="xs" class="q-mr-xs" />View Product
                                </a>
                                <a v-if="props.row.product_url"
                                   :href="props.row.product_url" target="_blank"
                                   class="q-btn q-btn--flat q-btn--dense q-btn--actionable"
                                   style="text-decoration: none; color: inherit; font-size: 0.85rem; padding: 4px 8px;"
                                   @click.stop>
                                    <q-icon name="open_in_new" size="xs" class="q-mr-xs" />HomeDepot.com
                                </a>
                            </div>
                        </div>
                    </q-td>
                </q-tr>
            """)

    # Wire button to the refreshable now that it exists
    apply_btn.on_click(alert_table.refresh)

    await alert_table()
