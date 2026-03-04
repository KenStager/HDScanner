"""Format alert groups as Slack mrkdwn text for OpenClaw delivery."""

from __future__ import annotations

from hd.dashboard.components.formatters import fmt_price, stock_badge

# Emoji per alert type
_TYPE_EMOJI: dict[str, str] = {
    "PRICE_DROP": "\U0001f3f7\ufe0f",      # label/tag
    "CLEARANCE": "\U0001f516",              # bookmark
    "SPECIAL_BUY": "\u2b50",               # star
    "BACK_IN_STOCK": "\U0001f4e6",         # package
    "OOS": "\U0001f6ab",                   # prohibited
}


def _emoji(alert_type: str) -> str:
    return _TYPE_EMOJI.get(alert_type, "\U0001f514")  # bell fallback


def _format_group(g: dict) -> str:
    """Format a single alert group as Slack mrkdwn."""
    alert_type = g.get("alert_type", "")
    severity = g.get("severity", "")
    title = g.get("product_title", "") or g.get("item_id", "?")
    payload = g.get("payload") or {}
    store_alerts = g.get("store_alerts", [])
    store_ids = g.get("store_ids_display", "")

    lines: list[str] = []

    # Header line
    lines.append(f"{_emoji(alert_type)} *{alert_type}* ({severity}) — {title}")

    # Stores
    lines.append(f"Stores: {store_ids}")

    # Price info
    if alert_type == "PRICE_DROP":
        before = payload.get("before", {})
        after = payload.get("after", {})
        b_price = fmt_price(before.get("price_value"))
        a_price = fmt_price(after.get("price_value"))
        pct = payload.get("pct_drop")
        pct_str = f" (-{pct:.0f}%)" if pct else ""
        lines.append(f"{b_price} \u2192 {a_price}{pct_str}")
    elif alert_type == "CLEARANCE":
        after = payload.get("after", {})
        a_price = fmt_price(after.get("price_value"))
        pct_off = after.get("percentage_off")
        pct_str = f" ({pct_off}% off)" if pct_off else ""
        lines.append(f"{a_price}{pct_str}")
    elif alert_type in ("OOS", "BACK_IN_STOCK"):
        before = payload.get("before", {})
        after = payload.get("after", {})
        b_label, _ = stock_badge(before.get("in_stock"))
        a_label, _ = stock_badge(after.get("in_stock"))
        lines.append(f"{b_label} \u2192 {a_label}")
    elif alert_type == "SPECIAL_BUY":
        after = payload.get("after", {})
        a_price = fmt_price(after.get("price_value"))
        lines.append(f"Special Buy at {a_price}")

    # Per-store stock info (when multiple stores)
    if len(store_alerts) > 1:
        stock_parts: list[str] = []
        for sa in sorted(store_alerts, key=lambda x: str(x.get("store_id", ""))):
            sp = sa.get("payload") or {}
            sa_after = sp.get("after", {})
            label, _ = stock_badge(sa_after.get("in_stock"))
            qty = sa_after.get("inventory_qty")
            sid = sa.get("store_id", "?")
            qty_str = f" / {qty} unit{'s' if qty != 1 else ''}" if qty is not None else ""
            stock_parts.append(f"{label}{qty_str} ({sid})")
        lines.append(f"Stock: {', '.join(stock_parts)}")
    else:
        # Single store — show stock if available
        sa = store_alerts[0] if store_alerts else {}
        sp = sa.get("payload") or {}
        sa_after = sp.get("after", {})
        in_stock = sa_after.get("in_stock")
        qty = sa_after.get("inventory_qty")
        if in_stock is not None:
            label, _ = stock_badge(in_stock)
            qty_str = f" / {qty} unit{'s' if qty != 1 else ''}" if qty is not None else ""
            lines.append(f"Stock: {label}{qty_str}")

    # Product link
    product_url = payload.get("product_url")
    if product_url:
        lines.append(f"<{product_url}|View on HomeDepot.com>")

    return "\n".join(lines)


def format_slack_message(groups: list[dict]) -> str:
    """Format a list of alert groups into a complete Slack mrkdwn message."""
    if not groups:
        return "*No new alerts.*"

    count = len(groups)
    header = f"*{count} new clearance alert{'s' if count != 1 else ''}*"

    parts = [header]
    for g in groups:
        parts.append(_format_group(g))

    return "\n\n".join(parts)
