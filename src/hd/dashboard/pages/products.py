"""Products page — searchable table + detail view with charts."""

from __future__ import annotations

from nicegui import ui

from hd.dashboard import _state
from hd.dashboard.components.charts import inventory_timeline_options, price_history_options
from hd.dashboard.components.formatters import (
    alert_type_icon,
    fmt_inventory_qty,
    fmt_pct,
    fmt_price,
    fmt_ts,
    format_alert_details,
    product_status_badge,
    severity_color,
    stock_badge,
)
from hd.dashboard.components.header import render_header
from hd.dashboard.queries import get_product_detail, get_products_with_latest


@ui.page("/products")
async def products_page() -> None:
    settings = _state.settings
    store_ids = settings.store_list
    render_header(settings.dashboard_title, current_path="/products")

    ui.add_css("""
        .q-table tbody tr:hover { background: rgba(255,255,255,0.05) !important; cursor: pointer; }
    """)

    products = await get_products_with_latest(settings, store_ids)

    # Build columns dynamically based on stores
    columns = [
        {"name": "status", "label": "Status", "field": "status_label", "sortable": True},
        {"name": "brand", "label": "Brand", "field": "brand", "sortable": True},
        {
            "name": "title",
            "label": "Title",
            "field": "title",
            "sortable": True,
            "style": "max-width: 350px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap",
            "headerStyle": "max-width: 350px",
        },
        {"name": "model", "label": "Model#", "field": "model_number", "sortable": True},
    ]
    for sid in store_ids:
        columns.append({
            "name": f"price_{sid}",
            "label": f"Price ({sid})",
            "field": f"price_{sid}",
            "sortable": True,
            "align": "right",
        })
        columns.append({
            "name": f"stock_{sid}",
            "label": f"Stock ({sid})",
            "field": f"stock_{sid}",
            "sortable": True,
        })

    # Build table rows
    rows = []
    for p in products:
        title = p.get("title", "")
        row = {
            "item_id": p["item_id"],
            "brand": p.get("brand", ""),
            "title": title,
            "model_number": p.get("model_number") or "",
        }
        # Collect per-store data for status badge
        savings_centers: list[str | None] = []
        price_pairs: list[tuple[float | None, float | None]] = []
        for sid in store_ids:
            current = p.get(f"price_{sid}")
            first = p.get(f"first_price_{sid}")
            row[f"price_{sid}"] = fmt_price(current)
            label, color = stock_badge(p.get(f"in_stock_{sid}"))
            row[f"stock_{sid}"] = label
            row[f"stock_color_{sid}"] = color
            savings_centers.append(p.get(f"savings_center_{sid}"))
            price_pairs.append((current, first))
            # Flag for price cell coloring
            row[f"price_dropped_{sid}"] = (
                current is not None and first is not None and current < first
            )
        badge = product_status_badge(savings_centers, price_pairs)
        row["status_label"] = badge[0] if badge else ""
        row["status_color"] = badge[1] if badge else ""
        rows.append(row)

    # Search filter
    filter_text = ui.input("Search", placeholder="Filter by brand, title, or model...").classes(
        "w-full max-w-md px-4"
    )

    # Scrollable table wrapper for smaller screens
    with ui.element("div").classes("w-full overflow-x-auto px-4"):
        table = ui.table(
            columns=columns,
            rows=rows,
            row_key="item_id",
            pagination=25,
        ).classes("w-full")

        # Status column badge
        table.add_slot(
            "body-cell-status",
            '''
            <q-td :props="props">
                <q-badge
                    v-if="props.row.status_label"
                    :color="props.row.status_color"
                    :label="props.row.status_label"
                />
            </q-td>
            ''',
        )

        # Color-code stock cells with badges + price cells when dropped
        for sid in store_ids:
            table.add_slot(
                f"body-cell-price_{sid}",
                f'''
                <q-td :props="props">
                    <span
                        :style="props.row.price_dropped_{sid} ? 'color: orange; font-weight: bold' : ''"
                    >{{{{ props.value }}}}</span>
                </q-td>
                ''',
            )
            table.add_slot(
                f"body-cell-stock_{sid}",
                f'''
                <q-td :props="props">
                    <q-badge
                        :color="props.row.stock_color_{sid}"
                        :label="props.value"
                    />
                </q-td>
                ''',
            )

    table.bind_filter_from(filter_text, "value")
    table.on("rowClick", lambda e: ui.navigate.to(f'/products/{e.args[1]["item_id"]}'))


@ui.page("/products/{item_id}")
async def product_detail_page(item_id: str) -> None:
    settings = _state.settings
    store_ids = settings.store_list
    render_header(settings.dashboard_title, current_path="/products")

    detail = await get_product_detail(settings, item_id)
    product = detail.get("product")

    if not product:
        ui.label("Product not found.").classes("text-h5 text-red px-4")
        return

    # Back link
    ui.link("← Back to Products", "/products").classes("px-4")

    # Product info
    with ui.row().classes("items-center gap-4 px-4 mt-2"):
        ui.label(product["title"]).classes("text-h5 font-bold")
        ui.badge(product["brand"]).props("color=primary")
        if product.get("model_number"):
            ui.label(f"Model: {product['model_number']}").classes("text-grey")
    if product.get("first_seen_ts"):
        ui.label(f"First seen: {fmt_ts(product['first_seen_ts'])}").classes("text-grey px-4")
    if product.get("canonical_url"):
        ui.link("View on HomeDepot.com", f"https://www.homedepot.com{product['canonical_url']}").classes("px-4")

    snapshots = detail.get("snapshots", [])

    # Current price/stock summary
    if snapshots:
        latest_by_store: dict[str, dict] = {}
        # Build first_by_store from the ASC-ordered snapshots at zero extra cost.
        # The first snapshot per store is the temporal price baseline. We use this
        # instead of the API's percentage_off, which measures "current price vs sum
        # of individual tool prices" for combo kits — a structural feature, not a
        # real discount signal.
        first_by_store: dict[str, dict] = {}
        for snap in snapshots:
            sid = snap.get("store_id", "")
            if sid in store_ids:
                # snapshots is ASC-ordered, so the first time we see a store_id
                # is guaranteed to be the oldest snapshot for that store
                if sid not in first_by_store:
                    first_by_store[sid] = snap
                existing = latest_by_store.get(sid)
                if existing is None or snap["ts"] > existing["ts"]:
                    latest_by_store[sid] = snap

        if latest_by_store:
            with ui.element("div").classes("w-full px-4 mt-4 grid grid-cols-1 sm:grid-cols-2 gap-4"):
                for sid in store_ids:
                    snap = latest_by_store.get(sid)
                    first_snap = first_by_store.get(sid)
                    if snap:
                        with ui.card().classes("p-4"):
                            ui.label(f"Store {sid}").classes("text-subtitle1 font-bold")
                            with ui.row().classes("items-center gap-4 mt-1"):
                                ui.label(fmt_price(snap.get("price_value"))).classes("text-h5 font-bold")
                                label, color = stock_badge(snap.get("in_stock"))
                                ui.badge(label).props(f"color={color}")
                                qty = snap.get("inventory_qty")
                                if qty is not None and qty > 0:
                                    ui.label(f"{qty} units").classes("text-grey")
                            if first_snap and first_snap.get("price_value") is not None:
                                ui.label(
                                    f"First seen at {fmt_price(first_snap['price_value'])}"
                                ).classes("text-sm text-grey mt-1")
                            # Show a discount badge only for trustworthy signals:
                            # 1. CLEARANCE: HD's own designation — percentage_off is the
                            #    markdown from the actual pre-clearance retail price.
                            # 2. Observed drop: current price is below our first recorded
                            #    price for this product at this store. This is temporally
                            #    grounded and ignores structural bundle offsets.
                            current_price = snap.get("price_value")
                            baseline_price = first_snap.get("price_value") if first_snap else None
                            if snap.get("savings_center") == "CLEARANCE" and snap.get("percentage_off"):
                                ui.badge(
                                    f"{snap['percentage_off']}% off (clearance)"
                                ).props("color=red")
                            elif (
                                current_price is not None
                                and baseline_price is not None
                                and current_price < baseline_price
                            ):
                                observed_drop = (baseline_price - current_price) / baseline_price * 100
                                ui.badge(
                                    f"{observed_drop:.0f}% below baseline"
                                ).props("color=orange")

    # Charts — responsive grid
    if snapshots:
        # Compute baseline_price as the max first-seen price across stores
        baseline_vals = [
            fs.get("price_value")
            for fs in first_by_store.values()
            if fs.get("price_value") is not None
        ] if first_by_store else []
        chart_baseline = max(baseline_vals) if baseline_vals else None

        with ui.element("div").classes("w-full px-4 mt-4 grid grid-cols-1 md:grid-cols-2 gap-4"):
            with ui.card():
                ui.label("Price History").classes("text-h6")
                ui.echart(price_history_options(snapshots, store_ids, baseline_price=chart_baseline)).classes("w-full h-64")
            with ui.card():
                ui.label("Inventory Timeline").classes("text-h6")
                ui.echart(inventory_timeline_options(snapshots, store_ids)).classes("w-full h-64")
    else:
        ui.label("No snapshot data available.").classes("px-4 text-grey mt-4")

    # Alert history
    alerts_list = detail.get("alerts", [])
    if alerts_list:
        ui.label("Alert History").classes("text-h6 px-4 mt-4")
        alert_columns = [
            {"name": "time", "label": "Time", "field": "time", "sortable": True},
            {"name": "store", "label": "Store", "field": "store_id", "sortable": True},
            {"name": "type", "label": "Type", "field": "alert_type", "sortable": True},
            {"name": "severity", "label": "Severity", "field": "severity", "sortable": True},
            {"name": "details", "label": "Details", "field": "details"},
        ]
        alert_rows = [
            {
                "time": fmt_ts(a["ts"]),
                "store_id": a["store_id"],
                "alert_type": a["alert_type"],
                "severity": a["severity"],
                "details": format_alert_details(a["alert_type"], a.get("payload")),
            }
            for a in alerts_list
        ]
        ui.table(columns=alert_columns, rows=alert_rows, row_key="time").classes("w-full px-4")
