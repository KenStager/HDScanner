"""Deal Board — the landing page.

The page's single job: show what's worth acting on right now. Three tabs:
one per store (in-store clearance shelf tags) plus ONLINE (special buys and
price drops, each showing HD's claimed cut next to the true cut against our
own 30-day price history). Every card can be dismissed as "not real" — it
stays hidden unless the deal later gets deeper than when it was dismissed.
"""

from __future__ import annotations

import html as _html

from nicegui import ui

from hd.dashboard import _state
from hd.dashboard.components.formatters import (
    fmt_history_span,
    fmt_low_date,
    fmt_ts_relative,
)
from hd.dashboard.components.cards import online_card_html as _online_card_html
from hd.dashboard.components.header import render_header
from hd.dashboard.components.health import render_health_banner
from hd.dashboard.queries import (
    ONLINE_STORE_KEY,
    dismiss_deal,
    get_daily_deal_picks,
    get_deal_board,
    get_online_deals,
    get_overview_stats,
    restore_deal,
)

ONLINE_TAB = ONLINE_STORE_KEY


@ui.page("/")
async def overview_page() -> None:
    settings = _state.settings
    render_header(settings.dashboard_title, current_path="/")
    await render_health_banner(settings)

    # Per-client view state
    view = {"store": None, "min_pct": 0, "new_only": False, "sort": "deepest",
            "show_hidden": False}

    async def _dismiss(store_id: str, item_id: str, value: float | None) -> None:
        await dismiss_deal(settings, store_id, item_id, value)
        content.refresh()

    async def _restore(store_id: str, item_id: str) -> None:
        await restore_deal(settings, store_id, item_id)
        content.refresh()

    @ui.refreshable
    async def content() -> None:
        stats = await get_overview_stats(settings)
        board = await get_deal_board(settings)
        online = await get_online_deals(settings)
        daily = await get_daily_deal_picks(settings)
        # A pick pinned in the daily strip must not show a second card below.
        daily_ids = {d["item_id"] for d in daily}
        online = [d for d in online if d["item_id"] not in daily_ids]
        daily_visible = [d for d in daily if not d["dismissed"]]
        deals_by_store: dict[str, list] = board["stores"]
        hidden_by_store: dict[str, list] = board.get("hidden", {})
        store_names: dict[str, str] = board["store_names"]

        store_ids = [s for s in settings.store_list if s in deals_by_store]
        store_ids += [s for s in deals_by_store if s not in store_ids]
        tabs = store_ids + [ONLINE_TAB]
        if view["store"] not in tabs:
            view["store"] = tabs[0] if tabs else None

        _status_line(stats)

        online_visible = [d for d in online if not d["dismissed"]]
        online_hidden = [d for d in online if d["dismissed"]]
        # The grid is the best of the best; "we saw it cheaper" cards live in
        # a small labeled strip below it, not in grid slots.
        online_grid = [d for d in online_visible if d.get("tier") != "warned"]
        online_warned = [d for d in online_visible if d.get("tier") == "warned"]

        # Tabs: one per store + ONLINE
        with ui.row().classes("w-full px-6 gap-8 mt-2"):
            for sid in store_ids:
                name = store_names.get(sid) or f"Store {sid}"
                count = len(deals_by_store.get(sid, []))
                active = "active" if sid == view["store"] else ""
                ui.html(
                    f'<button class="hd-storetab {active}">'
                    f'{_html.escape(name)} <span class="count">{count}</span></button>'
                ).on("click", lambda _, s=sid: _set(view, "store", s, content))
            active = "active" if view["store"] == ONLINE_TAB else ""
            ui.html(
                f'<button class="hd-storetab {active}">'
                f'Online <span class="count">{len(online_grid) + len(daily_visible)}</span></button>'
            ).on("click", lambda _: _set(view, "store", ONLINE_TAB, content))

        is_online = view["store"] == ONLINE_TAB

        # Filter chips
        hidden_here = online_hidden if is_online else hidden_by_store.get(view["store"], [])
        with ui.row().classes("w-full px-6 gap-2 items-center mt-1"):
            _chip("All", view["min_pct"] == 0 and not view["new_only"],
                  lambda: _reset_filters(view, content))
            _chip("25%+ off", view["min_pct"] == 25,
                  lambda: _set(view, "min_pct", 25, content))
            _chip("50%+ off", view["min_pct"] == 50,
                  lambda: _set(view, "min_pct", 50, content))
            if not is_online:
                _chip("New today", view["new_only"],
                      lambda: _set(view, "new_only", not view["new_only"], content))
            ui.element("div").classes("grow")
            if hidden_here:
                _chip(f"Hidden {len(hidden_here)}", view["show_hidden"],
                      lambda: _set(view, "show_hidden", not view["show_hidden"], content))
            if not is_online:
                _chip("Deepest cut", view["sort"] == "deepest",
                      lambda: _set(view, "sort", "deepest", content))
                _chip("Newest", view["sort"] == "newest",
                      lambda: _set(view, "sort", "newest", content))

        if is_online:
            deals = online_grid
            if view["min_pct"]:
                deals = [d for d in deals
                         if max(d["claimed_pct"], d.get("evidence_pct") or 0)
                         >= view["min_pct"]]
        else:
            deals = list(deals_by_store.get(view["store"], []))
            if view["min_pct"]:
                deals = [d for d in deals if (d["pct_off"] or 0) >= view["min_pct"]]
            if view["new_only"]:
                deals = [d for d in deals if d["is_new"]]
            if view["sort"] == "newest":
                deals.sort(key=lambda d: d["first_seen_ts"] or d["snapshot_ts"], reverse=True)

        store_key = ONLINE_TAB if is_online else view["store"]

        # Today's Daily Deals — pinned above the grid, exempt from the filter
        # chips and slot caps. Every pick carries our verdict, favorable or
        # not: the strip says what HD is pushing today, the chips say what our
        # record makes of it.
        if is_online and daily_visible:
            with ui.element("div").classes("w-full px-6 mt-4 mb-2"):
                with ui.element("div").classes("hd-daily-panel w-full"):
                    ui.html('<div class="hd-section-label">'
                            'Today\'s daily deals — HD\'s picks, our verdicts</div>') \
                        .classes("w-full")
                    with ui.element("div").classes("deal-grid w-full mt-3"):
                        for d in daily_visible:
                            _render_deal(d, True, _dismiss, ONLINE_TAB)

        if deals:
            with ui.element("div").classes("deal-grid w-full px-6 mt-3"):
                for d in deals:
                    _render_deal(d, is_online, _dismiss, store_key)
        else:
            with ui.column().classes("w-full items-center py-12"):
                label = "No online deals right now" if is_online and not online_grid \
                    else "Nothing matches these filters"
                ui.label(label).classes("text-grey")

        # The warning strip: HD claims a discount, we watched it sell for less.
        # Still possibly today's best available price — shown, but apart.
        if is_online and online_warned:
            ui.html('<div class="hd-section-label px-6 mt-6">'
                    'HD claims — we\'ve seen these cheaper</div>') \
                .classes("w-full")
            with ui.element("div").classes("deal-grid w-full px-6 mt-2"):
                for d in online_warned:
                    _render_deal(d, True, _dismiss, ONLINE_TAB)

        # Hidden deals — dimmed, with restore
        if view["show_hidden"] and hidden_here:
            ui.html('<div class="hd-section-label px-6 mt-6">Hidden — marked not real</div>') \
                .classes("w-full")
            with ui.element("div").classes("deal-grid w-full px-6 mt-2"):
                for d in hidden_here:
                    _render_deal(d, is_online, _dismiss, store_key,
                                 hidden=True, restore=_restore)

    await content()
    ui.timer(settings.dashboard_refresh_seconds, content.refresh)


def _render_deal(d: dict, is_online: bool, dismiss, store_key: str,
                 hidden: bool = False, restore=None) -> None:
    """One deal card with its hide/restore control layered on top."""
    cap_days = _state.settings.deal_history_window_days
    html = _online_card_html(d, cap_days) if is_online else _deal_card_html(d)
    wrap_cls = "deal-wrap dimmed" if hidden else "deal-wrap"
    value = d["price"] if is_online else d["clearance_value"]

    with ui.element("div").classes(wrap_cls):
        # sanitize=False: NiceGUI's sanitizer drops target=, so the card opened
        # in the same tab. Safe here because every interpolated value in the
        # card builders is html-escaped where it is inserted.
        ui.html(html, sanitize=False)
        if hidden and restore is not None:
            ui.button(icon="visibility",
                      on_click=lambda _, s=store_key, i=d["item_id"]: restore(s, i)) \
                .props("round dense size=sm").classes("deal-hide") \
                .tooltip("Show this deal again")
        else:
            ui.button(icon="visibility_off",
                      on_click=lambda _, s=store_key, i=d["item_id"], v=value: dismiss(s, i, v)) \
                .props("round dense size=sm").classes("deal-hide") \
                .tooltip("Not a real deal — hide it")


def _status_line(stats: dict) -> None:
    """One quiet line: health dot, scan freshness, tallies, run control."""
    health = stats["health_status"]
    dot = {"OK": "ok", "STALE": "stale", "DEGRADED": "degraded"}.get(health, "stale")
    label = {
        "OK": f"Scanned {fmt_ts_relative(stats.get('latest_snapshot_ts'))}",
        "STALE": f"Last scan {fmt_ts_relative(stats.get('latest_snapshot_ts'))} — overdue",
        "DEGRADED": "Scanner degraded — check Alerts",
    }.get(health, "")

    with ui.row().classes("w-full px-6 pt-3 items-center gap-3 hd-status"):
        ui.html(f'<span class="hd-dot {dot}"></span>')
        ui.label(label)
        ui.label("·")
        ui.label(f"{stats['clearance_count']} clearance deals")
        ui.label("·")
        ui.label(f"{stats['active_products']:,} items watched")
        nxt = _next_scan_label()
        if nxt:
            ui.label("·")
            ui.label(nxt)
        ui.element("div").classes("grow")


def _next_scan_label() -> str:
    """When the schedule fires next.

    Replaces a "Scan now" button that promised an instant result and delivered
    a 10-30 minute one. People pressed it because nothing on the page said
    whether the scanner was still working; saying so is the actual fix.
    """
    from datetime import datetime

    try:
        from hd.setup_schedule import scan_slots

        slots = scan_slots()
        now = datetime.now()
        later = [s for s in slots if (s.hour, s.minute) > (now.hour, now.minute)]
        nxt = later[0] if later else slots[0]
    except Exception:
        return ""
    when = "next scan" if later else "next scan tomorrow"
    return f"{when} {nxt.hour:02d}:{nxt.minute:02d}"


def _deal_card_html(d: dict) -> str:
    pct = int(d["pct_off"] or 0)
    hot = " hot" if pct >= 50 else ""
    title = _html.escape(d["title"])
    url = f'/products/{_html.escape(str(d["item_id"]), quote=True)}'

    if d.get("image_url"):
        img = f'<img src="{_html.escape(d["image_url"], quote=True)}" alt="" loading="lazy">'
    else:
        img = '<div class="placeholder">🔧</div>'

    price = f"${d['clearance_value']:,.2f}"
    was = (
        f'<span class="deal-was">${d["online_price"]:,.2f}</span>'
        if d.get("online_price") and d["online_price"] > d["clearance_value"]
        else ""
    )

    chips = ""
    qty = d.get("qty")
    if qty is not None:
        if qty <= 2:
            chips += f'<span class="deal-chip low">{qty} left</span>'
        else:
            chips += f'<span class="deal-chip">{qty} in stock</span>'
    # NEW already says "recent" — show the seen-age chip only for older deals
    if d.get("is_new"):
        chips += '<span class="deal-chip new">NEW</span>'
    else:
        seen = fmt_ts_relative(d.get("first_seen_ts"))
        if seen and seen != "-":
            chips += f'<span class="deal-chip">seen {_html.escape(seen)}</span>'

    return (
        f'<a class="deal-card" href="{url}" target="_blank" rel="noopener">'
        f'<div class="deal-img">{img}</div>'
        f'<div class="deal-flash{hot}"><span>Clearance</span><span>{pct}% off</span></div>'
        f'<div class="deal-price-row"><span class="deal-price">{price}</span>{was}</div>'
        f'<div class="deal-title">{title}</div>'
        f'<div class="deal-foot">{chips}</div>'
        f'</a>'
    )


def _chip(label: str, active: bool, on_click) -> None:
    cls = "hd-chip active" if active else "hd-chip"
    ui.html(f'<button class="{cls}">{_html.escape(label)}</button>').on(
        "click", lambda _: on_click()
    )


def _set(view: dict, key: str, value, content) -> None:
    if key == "min_pct" and view.get(key) == value:
        value = 0  # tapping the active chip clears it
    view[key] = value
    content.refresh()


def _reset_filters(view: dict, content) -> None:
    view["min_pct"] = 0
    view["new_only"] = False
    content.refresh()
