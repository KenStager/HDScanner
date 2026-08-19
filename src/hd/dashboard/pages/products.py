"""Products page — searchable table + the per-item dossier.

The detail page is the destination every deal card links to, so it must never
contradict the card: the price it leads with is the price you'd actually pay
(in-store clearance when present), and every verdict comes from witnessed,
dated facts in item_price_stats — never a percentage computed across the
scanner's coverage gap.
"""

from __future__ import annotations

import html as _html
from datetime import datetime

from nicegui import ui

from hd.dashboard import _state
from hd.dashboard.components.charts import (
    ONLINE_SERIES_COLOR,
    STORE_SERIES_COLORS,
    inventory_timeline_options,
    online_prices_agree,
    price_history_options,
)
from hd.dashboard.components.formatters import (
    fmt_low_date,
    fmt_price,
    fmt_ts_relative,
    format_price_change,
    infer_in_stock,
    product_status_badge,
    stock_badge,
    store_price_verdict,
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


# --- detail page ------------------------------------------------------------


def _store_color(index: int) -> str:
    """Store hue by config position — the same assignment the charts use."""
    return STORE_SERIES_COLORS[index % len(STORE_SERIES_COLORS)]


def _fmt_event_ts(val) -> str:
    """Compact feed timestamp: 'Aug 19 · 12:12'."""
    if val is None:
        return ""
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return _html.escape(val[:16])
    return val.strftime("%b %-d · %H:%M")


def _identity_html(product: dict, hd_url: str) -> str:
    title = _html.escape(product.get("title") or f"Item {product['item_id']}")
    if product.get("image_url"):
        img = f'<img src="{_html.escape(product["image_url"], quote=True)}" alt="">'
    else:
        img = '<div class="placeholder">🔧</div>'

    meta_bits = []
    if product.get("brand"):
        meta_bits.append(f"<span>{_html.escape(product['brand'])}</span>")
    if product.get("model_number"):
        meta_bits.append(f"<span>Model {_html.escape(product['model_number'])}</span>")
    meta_bits.append(f"<span>Item {_html.escape(str(product['item_id']))}</span>")
    meta_bits.append(
        f'<a href="{_html.escape(hd_url, quote=True)}" target="_blank" '
        f'rel="noopener">HomeDepot.com ↗</a>'
    )

    return (
        '<div class="pd-identity">'
        f'<div class="pd-img">{img}</div>'
        '<div>'
        f'<h1 class="pd-title">{title}</h1>'
        f'<div class="pd-meta">{"".join(meta_bits)}</div>'
        '</div></div>'
    )


def _verdict_chip(effective_price: float | None, stats: dict | None) -> str:
    verdict = store_price_verdict(effective_price, stats)
    if verdict is None:
        return ""
    label, cls = verdict
    return f'<span class="deal-chip {cls}">{_html.escape(label)}</span>'


def _store_card_html(
    name: str,
    color: str,
    snap: dict | None,
    stats: dict | None,
    hd_url: str,
    store_url: str | None,
    show_price: bool = False,
) -> str:
    """One store's card: only facts that are true AT that store — the in-store
    clearance price, stock, and how to point homedepot.com at it.

    The site price is store-independent and lives on the Online card, so a
    store card never leads with it; attributing an online deal to a store
    claimed availability we hadn't checked. show_price=True is the fallback
    for the rare store-localized items, where each store's price genuinely is
    its own fact.
    """
    dot = f'<span class="pd-dot" style="background:{color}"></span>'
    head_name = f'<div class="pd-store-name">{dot}{_html.escape(name)}</div>'

    if snap is None:
        # A store that has never carried the item — checked and absent is a
        # fact, unlike the absent-evidence states we stay silent about.
        return (
            f'<div class="pd-store"><div class="pd-store-head">{head_name}</div>'
            '<div class="pd-empty" style="padding: 8px 16px 16px">'
            'Never observed at this store.</div></div>'
        )

    scanned = fmt_ts_relative(snap.get("ts"))
    head = (
        f'<div class="pd-store-head">{head_name}'
        f'<span class="pd-store-scan">scanned {_html.escape(scanned)}</span></div>'
    )

    online = snap.get("price_value")
    clearance = snap.get("clearance_value")

    # In-store clearance wears the shelf-tag flash (red when deep). HD's
    # claimed % rides inside the flash; our verdict chip sits by the price.
    flash = ""
    price_row = ""
    if clearance is not None:
        pct = snap.get("clearance_percentage_off")
        if pct is None and online and online > clearance:
            pct = round((online - clearance) / online * 100)
        hot = " hot" if (pct or 0) >= 50 else ""
        pct_str = f"{int(pct)}% off" if pct else ""
        flash = (
            f'<div class="pd-flash{hot}"><span>In-store clearance</span>'
            f'<span>{pct_str}</span></div>'
        )
        was = (
            f'<span class="pd-was">{fmt_price(online)}</span>'
            if online is not None and online > clearance else ""
        )
        price_row = (
            f'<div class="pd-price-row"><span class="pd-price">{fmt_price(clearance)}</span>'
            f'{was}{_verdict_chip(clearance, stats)}</div>'
        )
    elif show_price and online is not None:
        price_row = (
            f'<div class="pd-price-row"><span class="pd-price">{fmt_price(online)}</span>'
            f'{_verdict_chip(online, stats)}</div>'
        )

    in_stock = infer_in_stock(snap)
    stock_label, _ = stock_badge(in_stock)
    stock_dot_cls = {True: "ok", False: "degraded"}.get(in_stock, "stale")
    qty = snap.get("inventory_qty")
    qty_str = f" · {qty} units" if qty is not None and qty > 0 else ""
    stock = (
        f'<div class="pd-stock"><span class="hd-dot {stock_dot_cls}"></span>'
        f'{_html.escape(stock_label)}{qty_str}</div>'
    )

    # Two links, because homedepot.com localizes by cookie and honours no
    # store query parameter: the product opens in whatever store the browser
    # holds, and the store page's "Shop This Store" button is the only way to
    # switch it.
    links = f'<a href="{_html.escape(hd_url, quote=True)}" target="_blank" rel="noopener">Open on HomeDepot.com ↗</a>'
    if store_url:
        links += (
            f'<br><a href="{_html.escape(store_url, quote=True)}" target="_blank" '
            f'rel="noopener">Set HD to {_html.escape(name)} ↗</a> '
            '<span class="hint">then click “Shop This Store”</span>'
        )

    return (
        f'<div class="pd-store">{head}{flash}{price_row}{stock}'
        f'<div class="pd-store-links">{links}</div></div>'
    )


def _online_card_html(snap: dict, stats: dict | None, hd_url: str) -> str:
    """The site price as its own entity — the page's mirror of the deal
    board's Online tab. Store-independent, so it makes no availability claim;
    the store cards carry stock."""
    dot = f'<span class="pd-dot" style="background:{ONLINE_SERIES_COLOR}"></span>'
    scanned = fmt_ts_relative(snap.get("ts"))
    head = (
        f'<div class="pd-store-head"><div class="pd-store-name">{dot}Online</div>'
        f'<span class="pd-store-scan">scanned {_html.escape(scanned)}</span></div>'
    )

    price = snap.get("price_value")
    original = snap.get("price_original")

    flash = ""
    pct = snap.get("percentage_off") or 0
    if not pct and original and price and original > price:
        pct = round((original - price) / original * 100)
    if snap.get("special_buy") or snap.get("savings_center") == "CLEARANCE" or pct > 0:
        label = "Special Buy" if snap.get("special_buy") else (
            "Clearance" if snap.get("savings_center") == "CLEARANCE" else "Online deal"
        )
        pct_str = f"{int(pct)}% off" if pct else ""
        flash = (
            f'<div class="pd-flash online"><span>{label}</span>'
            f'<span>{pct_str}</span></div>'
        )

    was = (
        f'<span class="pd-was">{fmt_price(original)}</span>'
        if original is not None and price is not None and original > price else ""
    )
    price_row = (
        f'<div class="pd-price-row"><span class="pd-price">{fmt_price(price)}</span>'
        f'{was}{_verdict_chip(price, stats)}</div>'
    )

    links = (
        f'<a href="{_html.escape(hd_url, quote=True)}" target="_blank" '
        f'rel="noopener">Open on HomeDepot.com ↗</a>'
    )
    return (
        f'<div class="pd-store">{head}{flash}{price_row}'
        f'<div class="pd-store-links">{links}</div></div>'
    )


def _record_row_html(name: str, color: str, stats: dict) -> str:
    """One store's witnessed price facts — dated, gap-immune, evidence-labeled."""
    dot = f'<span class="pd-dot" style="background:{color}"></span>'
    store = f'<div class="pd-record-store">{dot}{_html.escape(name)}</div>'

    low, high = stats.get("low_price"), stats.get("high_price")
    obs_days = stats.get("obs_days") or 0
    first = fmt_low_date(stats.get("first_ts"))
    watched_d = f'since {first}' if first else ""
    watched = (
        '<div class="pd-fact"><span class="k">Watched</span>'
        f'<span class="v">{obs_days} day{"s" if obs_days != 1 else ""}</span>'
        f'<span class="d">{_html.escape(watched_d)}</span></div>'
    )

    if low is None:
        return f'<div class="pd-record-row">{store}{watched}</div>'

    if obs_days <= 1:
        # A single observation sets low and high by definition — presenting
        # them as extremes would dress one data point as a record.
        note = (
            f'<span class="pd-record-note">Single observation at {fmt_price(low)}'
            f'{" · " + _html.escape(first) if first else ""}</span>'
        )
        return f'<div class="pd-record-row">{store}{note}</div>'

    if low == high:
        note = f'<span class="pd-record-note">Every observation at {fmt_price(low)}</span>'
        return f'<div class="pd-record-row">{store}{note}{watched}</div>'

    low_d = fmt_low_date(stats.get("low_ts"))
    high_d = fmt_low_date(stats.get("high_ts"))
    facts = (
        '<div class="pd-fact"><span class="k">Lowest</span>'
        f'<span class="v">{fmt_price(low)}</span>'
        f'<span class="d">{_html.escape(low_d)}</span></div>'
        '<div class="pd-fact"><span class="k">Highest</span>'
        f'<span class="v">{fmt_price(high)}</span>'
        f'<span class="d">{_html.escape(high_d)}</span></div>'
    )
    return f'<div class="pd-record-row">{store}{facts}{watched}</div>'


def _feed_html(alerts: list[dict], store_names: dict[str, str], limit: int = 30) -> str:
    rows = []
    for a in alerts[:limit]:
        what = format_price_change(a["alert_type"], a.get("payload")) \
            or a["alert_type"].replace("_", " ").title()
        sev = a.get("severity") or ""
        store = store_names.get(a["store_id"]) or f"Store {a['store_id']}"
        rows.append(
            '<div class="pd-feed-row">'
            f'<span class="pd-feed-when">{_fmt_event_ts(a["ts"])}</span>'
            f'<span class="pd-feed-store">{_html.escape(store)}</span>'
            f'<span class="pd-feed-what"><span class="sev {_html.escape(sev)}"></span>'
            f'{_html.escape(what)}</span></div>'
        )
    more = ""
    if len(alerts) > limit:
        more = f'<div class="pd-empty">… {len(alerts) - limit} older events not shown</div>'
    return f'<div class="pd-feed">{"".join(rows)}{more}</div>'


def _low_anchor(
    price_stats: dict[str, dict], snapshots: list[dict]
) -> tuple[float, str] | None:
    """The witnessed low as a chart reference — only when it adds information,
    i.e. when it sits below every price the window already draws."""
    lows = [
        (s["low_price"], s.get("low_ts"))
        for s in price_stats.values()
        if s.get("low_price") is not None and (s.get("obs_days") or 0) >= 2
    ]
    if not lows:
        return None
    low, low_ts = min(lows, key=lambda t: t[0])
    window_prices = [
        p for s in snapshots
        for p in (s.get("price_value"), s.get("clearance_value"))
        if p is not None
    ]
    if not window_prices or low >= min(window_prices):
        return None
    when = fmt_low_date(low_ts)
    label = f"seen ${low:,.2f}" + (f" · {when}" if when else "")
    return (low, label)


def _section_label(text: str) -> None:
    ui.html(f'<div class="hd-section-label">{_html.escape(text)}</div>') \
        .classes("w-full mt-6")


@ui.page("/products/{item_id}")
async def product_detail_page(item_id: str) -> None:
    settings = _state.settings
    store_ids = settings.store_list
    render_header(settings.dashboard_title, current_path="/products")

    detail = await get_product_detail(settings, item_id)
    product = detail.get("product")
    store_names: dict[str, str] = detail.get("store_names", {})
    store_urls: dict[str, str] = detail.get("store_urls", {})
    price_stats: dict[str, dict] = detail.get("price_stats", {})
    snapshots = detail.get("snapshots", [])
    alerts_list = detail.get("alerts", [])

    with ui.element("div").classes("pd-wrap"):
        ui.html('<a class="pd-back" href="/products">← All products</a>', sanitize=False)

        if not product:
            ui.html(
                '<div class="pd-empty" style="padding-top: 24px">'
                'Product not found.</div>', sanitize=False,
            )
            return

        ui.page_title(product.get("title") or f"Item {item_id}")

        canonical = product.get("canonical_url")
        hd_url = (
            f"https://www.homedepot.com{canonical}"
            if canonical
            else f"https://www.homedepot.com/s/{product['item_id']}"
        )

        # sanitize=False throughout: NiceGUI's sanitizer strips target=, which
        # silently broke new-tab links. Safe because every interpolated value
        # is escaped where it is inserted.
        ui.html(_identity_html(product, hd_url), sanitize=False)

        # Latest snapshot per store — the "what would I pay today" cards
        latest_by_store: dict[str, dict] = {}
        for snap in snapshots:  # ASC-ordered, so the last seen wins
            sid = snap.get("store_id", "")
            if sid in store_ids:
                latest_by_store[sid] = snap

        # The site price is one fact when the stores agree on it — presented
        # as its own Online card, never attributed to a store (a store card
        # claiming an online deal implied availability we hadn't checked).
        # Store-localized items keep the price on each store card instead.
        agree = online_prices_agree(snapshots, store_ids)

        cards = "".join(
            _store_card_html(
                name=store_names.get(sid) or f"Store {sid}",
                color=_store_color(i),
                snap=latest_by_store.get(sid),
                stats=price_stats.get(sid),
                hd_url=hd_url,
                store_url=store_urls.get(sid),
                show_price=not agree,
            )
            for i, sid in enumerate(store_ids)
        )
        if agree and latest_by_store:
            freshest = max(latest_by_store.values(), key=lambda s: s["ts"])
            if freshest.get("price_value") is not None:
                cards += _online_card_html(
                    freshest, price_stats.get(freshest.get("store_id", "")), hd_url
                )
        ui.html(f'<div class="pd-stores">{cards}</div>', sanitize=False)

        # Price record — the durable witnessed facts. Stores that witnessed
        # the identical record collapse into one "Online price" row; absent
        # records get silence, not a placeholder.
        def _fact_key(stats: dict) -> tuple:
            return (
                stats.get("low_price"), fmt_low_date(stats.get("low_ts")),
                stats.get("high_price"), fmt_low_date(stats.get("high_ts")),
                stats.get("obs_days"), fmt_low_date(stats.get("first_ts")),
            )

        present = [(i, sid) for i, sid in enumerate(store_ids) if sid in price_stats]
        if agree and present and len({_fact_key(price_stats[sid]) for _, sid in present}) == 1:
            record_rows = _record_row_html(
                "Online price", ONLINE_SERIES_COLOR, price_stats[present[0][1]]
            )
        else:
            record_rows = "".join(
                _record_row_html(
                    store_names.get(sid) or f"Store {sid}", _store_color(i),
                    price_stats[sid],
                )
                for i, sid in present
            )
        if record_rows:
            _section_label("Price record")
            ui.html(f'<div class="pd-record mt-2">{record_rows}</div>', sanitize=False)

        # Charts — the 90-day window, honestly labeled as such
        priced = [s for s in snapshots if s.get("price_value") is not None
                  or s.get("clearance_value") is not None]
        if priced:
            _section_label("Price · last 90 days")
            with ui.element("div").classes("pd-panel w-full"):
                ui.echart(price_history_options(
                    snapshots, store_ids, store_names,
                    low_anchor=_low_anchor(price_stats, snapshots),
                )).classes("w-full h-72")

        if any(s.get("inventory_qty") is not None for s in snapshots):
            _section_label("Inventory · last 90 days")
            with ui.element("div").classes("pd-panel w-full"):
                ui.echart(
                    inventory_timeline_options(snapshots, store_ids, store_names)
                ).classes("w-full h-72")

        if alerts_list:
            _section_label("Activity")
            ui.html(_feed_html(alerts_list, store_names), sanitize=False)

        ui.element("div").classes("h-8")
