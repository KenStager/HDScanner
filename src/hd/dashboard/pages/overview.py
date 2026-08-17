"""Deal Board — the landing page.

The page's single job: show what's worth acting on right now. Three tabs:
one per store (in-store clearance shelf tags) plus ONLINE (special buys and
price drops, each showing HD's claimed cut next to the true cut against our
own 30-day price history). Every card can be dismissed as "not real" — it
stays hidden unless the deal later gets deeper than when it was dismissed.
"""

from __future__ import annotations

import asyncio
import html as _html

from nicegui import ui

from hd.dashboard import _state
from hd.dashboard.components.formatters import fmt_ts, fmt_ts_relative
from hd.dashboard.components.header import render_header
from hd.dashboard.pipeline_runner import run_pipeline_background
from hd.dashboard.queries import (
    ONLINE_STORE_KEY,
    dismiss_deal,
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
                f'Online <span class="count">{len(online_visible)}</span></button>'
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
            deals = online_visible
            if view["min_pct"]:
                deals = [d for d in deals
                         if max(d["claimed_pct"], d["true_pct"]) >= view["min_pct"]]
        else:
            deals = list(deals_by_store.get(view["store"], []))
            if view["min_pct"]:
                deals = [d for d in deals if (d["pct_off"] or 0) >= view["min_pct"]]
            if view["new_only"]:
                deals = [d for d in deals if d["is_new"]]
            if view["sort"] == "newest":
                deals.sort(key=lambda d: d["first_seen_ts"] or d["snapshot_ts"], reverse=True)

        store_key = ONLINE_TAB if is_online else view["store"]
        if deals:
            with ui.element("div").classes("deal-grid w-full px-6 mt-3"):
                for d in deals:
                    _render_deal(d, is_online, _dismiss, store_key)
        else:
            with ui.column().classes("w-full items-center py-12"):
                label = "No online deals right now" if is_online and not online_visible \
                    else "Nothing matches these filters"
                ui.label(label).classes("text-grey")

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
    html = _online_card_html(d) if is_online else _deal_card_html(d)
    wrap_cls = "deal-wrap dimmed" if hidden else "deal-wrap"
    value = d["price"] if is_online else d["clearance_value"]

    with ui.element("div").classes(wrap_cls):
        ui.html(html)
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

    ps = _state.pipeline_state
    with ui.row().classes("w-full px-6 pt-3 items-center gap-3 hd-status"):
        ui.html(f'<span class="hd-dot {dot}"></span>')
        ui.label(label)
        ui.label("·")
        ui.label(f"{stats['clearance_count']} clearance deals")
        ui.label("·")
        ui.label(f"{stats['active_products']:,} items watched")
        ui.element("div").classes("grow")
        if ps.is_running:
            ui.spinner(size="xs")
            ui.label("Scanning…")
        else:
            ui.button("Scan now", icon="radar",
                      on_click=lambda: _trigger_pipeline(_state.settings, None)) \
                .props("flat dense size=sm no-caps").classes("text-orange")
            if ps.last_run_error:
                ui.label(f"Last run failed at {fmt_ts(ps.last_run_ts)}").classes("text-red")


def _deal_card_html(d: dict) -> str:
    pct = int(d["pct_off"] or 0)
    hot = " hot" if pct >= 50 else ""
    title = _html.escape(d["title"])
    url = _html.escape(d["url"], quote=True)

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


def _online_card_html(d: dict) -> str:
    """Online deal shelf tag: HD's claim on the flash, the truth in the chips."""
    title = _html.escape(d["title"])
    url = _html.escape(d["url"], quote=True)

    if d.get("image_url"):
        img = f'<img src="{_html.escape(d["image_url"], quote=True)}" alt="" loading="lazy">'
    else:
        img = '<div class="placeholder">🔧</div>'

    price = f"${d['price']:,.2f}"
    was = (
        f'<span class="deal-was">${d["original"]:,.2f}</span>'
        if d.get("original") and d["original"] > d["price"]
        else ""
    )

    label = "Special Buy" if d.get("special_buy") else "Online Deal"
    claimed = int(d["claimed_pct"] or 0)
    true_pct = int(d["true_pct"] or 0)

    # The honesty chip: our 30-day history vs HD's claimed discount
    if true_pct >= 10:
        truth = f'<span class="deal-chip true">true −{true_pct}% vs 30d</span>'
    elif d.get("high_30d") is not None:
        truth = '<span class="deal-chip flat">flat 30d price</span>'
    else:
        truth = '<span class="deal-chip">no price history</span>'

    chips = truth
    if d.get("high_30d") and d["high_30d"] > d["price"]:
        chips += f'<span class="deal-chip">high ${d["high_30d"]:,.0f}</span>'

    return (
        f'<a class="deal-card" href="{url}" target="_blank" rel="noopener">'
        f'<div class="deal-img">{img}</div>'
        f'<div class="deal-flash online"><span>{label}</span><span>{claimed}% off</span></div>'
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


def _trigger_pipeline(settings, _content) -> None:
    """Start the pipeline in the background and notify on completion."""
    if _state.pipeline_state.is_running:
        ui.notification("A scan is already running", type="warning")
        return

    async def _run_and_refresh():
        await run_pipeline_background(settings)
        ps = _state.pipeline_state
        if ps.last_run_error:
            ui.notification(f"Scan failed: {ps.last_run_error}", type="negative")
        else:
            r = ps.last_run_result or {}
            ui.notification(
                f"Scan complete: {r.get('snapshots', 0)} prices checked, "
                f"{r.get('alerts', 0)} new alerts",
                type="positive",
            )

    ui.notification("Scan started", type="info")
    asyncio.create_task(_run_and_refresh())
