"""Format alert groups as Slack Block Kit cards."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hd.dashboard.components.formatters import fmt_price, stock_badge, infer_in_stock

# Emoji per alert type
_TYPE_EMOJI: dict[str, str] = {
    "PRICE_DROP": "\U0001f3f7\ufe0f",      # label/tag
    "CLEARANCE": "\U0001f516",              # bookmark
    "SPECIAL_BUY": "\u2b50",               # star
    "DEEP_DISCOUNT": "\U0001f525",         # fire
    "PRICING_ERROR": "\U0001f6a8",          # rotating light
    "IN_STORE_CLEARANCE": "\U0001f3ea",     # convenience store
    "BACK_IN_STOCK": "\U0001f4e6",         # package
    "OOS": "\U0001f6ab",                   # prohibited
}


def _emoji(alert_type: str) -> str:
    return _TYPE_EMOJI.get(alert_type, "\U0001f514")  # bell fallback



def _store_price_line(sa: dict, alert_type: str) -> str:
    """One-line price summary from a single store_alert's payload."""
    sp = sa.get("payload") or {}
    before = sp.get("before", {})
    after = sp.get("after", {})

    if alert_type == "PRICE_DROP":
        b_price = fmt_price(before.get("price_value"))
        a_price = fmt_price(after.get("price_value"))
        pct = sp.get("pct_drop")
        pct_str = f" (-{pct:.0f}%)" if pct else ""
        return f"{b_price} → {a_price}{pct_str}"
    elif alert_type == "CLEARANCE":
        a_price = fmt_price(after.get("price_value"))
        pct_off = sp.get("percentage_off") or after.get("percentage_off")
        pct_str = f" ({pct_off}% off)" if pct_off else ""
        return f"{a_price}{pct_str}"
    elif alert_type == "SPECIAL_BUY":
        a_price = fmt_price(after.get("price_value"))
        pct_off = sp.get("percentage_off") or sp.get("observed_pct_off")
        pct_str = f" (-{pct_off:.0f}%)" if pct_off else ""
        return f"Special Buy at {a_price}{pct_str}"
    elif alert_type == "DEEP_DISCOUNT":
        a_price = fmt_price(after.get("price_value"))
        pct_off = sp.get("percentage_off") or after.get("percentage_off")
        pct_str = f" ({pct_off}% off)" if pct_off else ""
        return f"{a_price}{pct_str}"
    elif alert_type == "PRICING_ERROR":
        a_price = fmt_price(after.get("price_value"))
        ref = sp.get("reference_price")
        ref_price = fmt_price(ref)
        pct = sp.get("pct_off_reference")
        pct_str = f", {pct:.0f}% off" if pct else ""
        return f"{a_price} (ref: {ref_price}{pct_str})"
    elif alert_type == "IN_STORE_CLEARANCE":
        cl_price = fmt_price(sp.get("clearance_value"))
        online = fmt_price(after.get("price_value"))
        pct = sp.get("clearance_percentage_off")
        pct_str = f" ({pct}% off)" if pct else ""
        return f"In-store: {cl_price}{pct_str} / Online: {online}"
    return ""


def _prices_vary(store_alerts: list[dict], alert_type: str) -> bool:
    """Return True if store_alerts have different price lines."""
    if alert_type in ("OOS", "BACK_IN_STOCK"):
        return False
    lines = {_store_price_line(sa, alert_type) for sa in store_alerts}
    # Filter out empty strings (no price info)
    lines.discard("")
    return len(lines) > 1


def _build_group_blocks(g: dict) -> list[dict[str, Any]]:
    """Build Block Kit blocks for a single alert group."""
    alert_type = g.get("alert_type", "")
    severity = g.get("severity", "")
    title = g.get("product_title", "") or g.get("item_id", "?")
    payload = g.get("payload") or {}
    store_alerts = g.get("store_alerts", [])
    store_ids = g.get("store_ids_display", "")
    product_url = payload.get("product_url")

    blocks: list[dict[str, Any]] = []

    # Header
    header_text = f"{_emoji(alert_type)} *{title}*"
    header_block: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}
    image_url = payload.get("image_url")
    if image_url:
        slack_image_url = image_url.replace("_600.", "_300.")
        header_block["accessory"] = {
            "type": "image",
            "image_url": slack_image_url,
            "alt_text": title,
        }
    blocks.append(header_block)

    # Fields: type/severity, price, stores, stock
    fields: list[dict[str, str]] = []

    fields.append({"type": "mrkdwn", "text": f"*Type:*\n{alert_type} ({severity})"})
    fields.append({"type": "mrkdwn", "text": f"*Stores:*\n{store_ids}"})

    # Combined "By Store" layout when multi-store prices differ
    use_by_store = len(store_alerts) > 1 and _prices_vary(store_alerts, alert_type)

    if use_by_store:
        by_store_lines: list[str] = []
        for sa in sorted(store_alerts, key=lambda x: str(x.get("store_id", ""))):
            sid = sa.get("store_id", "?")
            price_line = _store_price_line(sa, alert_type)
            sp = sa.get("payload") or {}
            sa_after = sp.get("after", {})
            label, _ = stock_badge(infer_in_stock(sa_after))
            qty = sa_after.get("inventory_qty")
            qty_str = f" / {qty}" if qty is not None else ""
            by_store_lines.append(f"Store {sid}: {price_line} · {label}{qty_str}")
        fields.append({"type": "mrkdwn", "text": f"*By Store:*\n" + "\n".join(by_store_lines)})
    else:
        # Price field
        if alert_type == "PRICE_DROP":
            before = payload.get("before", {})
            after = payload.get("after", {})
            b_price = fmt_price(before.get("price_value"))
            a_price = fmt_price(after.get("price_value"))
            pct = payload.get("pct_drop")
            pct_str = f" (-{pct:.0f}%)" if pct else ""
            fields.append({"type": "mrkdwn", "text": f"*Price:*\n{b_price} → {a_price}{pct_str}"})
        elif alert_type == "CLEARANCE":
            after = payload.get("after", {})
            a_price = fmt_price(after.get("price_value"))
            pct_off = payload.get("percentage_off") or after.get("percentage_off")
            pct_str = f" ({pct_off}% off)" if pct_off else ""
            fields.append({"type": "mrkdwn", "text": f"*Price:*\n{a_price}{pct_str}"})
        elif alert_type == "SPECIAL_BUY":
            after = payload.get("after", {})
            a_price = fmt_price(after.get("price_value"))
            pct_off = payload.get("percentage_off") or payload.get("observed_pct_off")
            pct_str = f" (-{pct_off:.0f}%)" if pct_off else ""
            fields.append({"type": "mrkdwn", "text": f"*Price:*\nSpecial Buy at {a_price}{pct_str}"})
        elif alert_type == "DEEP_DISCOUNT":
            after = payload.get("after", {})
            a_price = fmt_price(after.get("price_value"))
            pct_off = payload.get("percentage_off") or after.get("percentage_off")
            pct_str = f" ({pct_off}% off)" if pct_off else ""
            fields.append({"type": "mrkdwn", "text": f"*Price:*\n{a_price}{pct_str}"})
        elif alert_type == "PRICING_ERROR":
            after = payload.get("after", {})
            ref = payload.get("reference_price")
            a_price = fmt_price(after.get("price_value"))
            ref_price = fmt_price(ref)
            pct = payload.get("pct_off_reference")
            pct_str = f", {pct:.0f}% off" if pct else ""
            fields.append({"type": "mrkdwn", "text": f"*Price:*\n{a_price} (ref: {ref_price}{pct_str})"})
        elif alert_type == "IN_STORE_CLEARANCE":
            after = payload.get("after", {})
            cl_price = fmt_price(payload.get("clearance_value"))
            online = fmt_price(after.get("price_value"))
            pct = payload.get("clearance_percentage_off")
            pct_str = f" ({pct}% off)" if pct else ""
            fields.append({"type": "mrkdwn", "text": f"*In-Store:*\n{cl_price}{pct_str}"})
            fields.append({"type": "mrkdwn", "text": f"*Online:*\n{online}"})
        elif alert_type in ("OOS", "BACK_IN_STOCK"):
            before = payload.get("before", {})
            after = payload.get("after", {})
            b_label, _ = stock_badge(before.get("in_stock"))
            a_label, _ = stock_badge(after.get("in_stock"))
            fields.append({"type": "mrkdwn", "text": f"*Status:*\n{b_label} → {a_label}"})

        # Stock field
        if len(store_alerts) > 1:
            stock_parts: list[str] = []
            for sa in sorted(store_alerts, key=lambda x: str(x.get("store_id", ""))):
                sp = sa.get("payload") or {}
                sa_after = sp.get("after", {})
                label, _ = stock_badge(infer_in_stock(sa_after))
                qty = sa_after.get("inventory_qty")
                sid = sa.get("store_id", "?")
                qty_str = f" / {qty}" if qty is not None else ""
                stock_parts.append(f"{label}{qty_str} ({sid})")
            fields.append({"type": "mrkdwn", "text": f"*Stock:*\n{', '.join(stock_parts)}"})
        else:
            sa = store_alerts[0] if store_alerts else {}
            sp = sa.get("payload") or {}
            sa_after = sp.get("after", {})
            in_stock = infer_in_stock(sa_after)
            qty = sa_after.get("inventory_qty")
            if in_stock is not None:
                label, _ = stock_badge(in_stock)
                qty_str = f" / {qty} units" if qty is not None else ""
                fields.append({"type": "mrkdwn", "text": f"*Stock:*\n{label}{qty_str}"})

    blocks.append({"type": "section", "fields": fields})

    # In-store only context for clearance
    if alert_type == "IN_STORE_CLEARANCE":
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": ":department_store: In-store only \u2014 visit store to purchase at clearance price"}]
        })

    # Daily deal warning for Special Buys
    if alert_type == "SPECIAL_BUY":
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": ":clock3: May be a Daily Deal \u2014 act fast, could expire in 24h"}]
        })

    # Link button
    if product_url:
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View on HomeDepot.com"},
                "url": product_url,
                "style": "primary",
            }],
        })

    return blocks


def format_slack_blocks(groups: list[dict]) -> tuple[list[dict[str, Any]], str]:
    """Format alert groups into Block Kit blocks and a fallback text string.

    Returns (blocks, fallback_text).
    """
    if not groups:
        return [], "No new alerts."

    count = len(groups)
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    header_text = f"{count} new alert{'s' if count != 1 else ''} — {date_str}"

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
    ]

    for i, g in enumerate(groups):
        blocks.extend(_build_group_blocks(g))
        if i < len(groups) - 1:
            blocks.append({"type": "divider"})

    # Slack limit: 50 blocks per message
    blocks = blocks[:50]

    return blocks, header_text


# Keep the plain-text formatter for CLI dry-run output
def format_slack_message(groups: list[dict]) -> str:
    """Format a list of alert groups into a plain mrkdwn string (for CLI preview)."""
    if not groups:
        return "*No new alerts.*"

    count = len(groups)
    header = f"*{count} new alert{'s' if count != 1 else ''}*"

    parts = [header]
    for g in groups:
        parts.append(_format_group_text(g))

    return "\n\n".join(parts)


def _format_group_text(g: dict) -> str:
    """Format a single alert group as plain mrkdwn text."""
    alert_type = g.get("alert_type", "")
    severity = g.get("severity", "")
    title = g.get("product_title", "") or g.get("item_id", "?")
    payload = g.get("payload") or {}
    store_alerts = g.get("store_alerts", [])
    store_ids = g.get("store_ids_display", "")

    lines: list[str] = []
    lines.append(f"{_emoji(alert_type)} *{alert_type}* ({severity}) — {title}")
    lines.append(f"Stores: {store_ids}")

    # Combined "By Store" layout when multi-store prices differ
    use_by_store = len(store_alerts) > 1 and _prices_vary(store_alerts, alert_type)

    if use_by_store:
        by_store_lines: list[str] = []
        for sa in sorted(store_alerts, key=lambda x: str(x.get("store_id", ""))):
            sid = sa.get("store_id", "?")
            price_line = _store_price_line(sa, alert_type)
            sp = sa.get("payload") or {}
            sa_after = sp.get("after", {})
            label, _ = stock_badge(infer_in_stock(sa_after))
            qty = sa_after.get("inventory_qty")
            qty_str = f" / {qty}" if qty is not None else ""
            by_store_lines.append(f"Store {sid}: {price_line} · {label}{qty_str}")
        lines.append("By Store:")
        lines.extend(by_store_lines)
    else:
        after = payload.get("after", {})
        if alert_type == "PRICE_DROP":
            before = payload.get("before", {})
            b_price = fmt_price(before.get("price_value"))
            a_price = fmt_price(after.get("price_value"))
            pct = payload.get("pct_drop")
            pct_str = f" (-{pct:.0f}%)" if pct else ""
            lines.append(f"{b_price} → {a_price}{pct_str}")
        elif alert_type == "CLEARANCE":
            a_price = fmt_price(after.get("price_value"))
            pct_off = payload.get("percentage_off") or after.get("percentage_off")
            pct_str = f" ({pct_off}% off)" if pct_off else ""
            lines.append(f"{a_price}{pct_str}")
        elif alert_type == "SPECIAL_BUY":
            pct_off = payload.get("percentage_off") or payload.get("observed_pct_off")
            pct_str = f" (-{pct_off:.0f}%)" if pct_off else ""
            lines.append(f"Special Buy at {fmt_price(after.get('price_value'))}{pct_str}")
        elif alert_type in ("OOS", "BACK_IN_STOCK"):
            before = payload.get("before", {})
            b_label, _ = stock_badge(before.get("in_stock"))
            a_label, _ = stock_badge(after.get("in_stock"))
            lines.append(f"{b_label} → {a_label}")
        elif alert_type == "DEEP_DISCOUNT":
            a_price = fmt_price(after.get("price_value"))
            pct_off = payload.get("percentage_off") or after.get("percentage_off")
            pct_str = f" ({pct_off}% off)" if pct_off else ""
            lines.append(f"Deep discount: {a_price}{pct_str}")
        elif alert_type == "IN_STORE_CLEARANCE":
            cl_price = fmt_price(payload.get("clearance_value"))
            online = fmt_price(after.get("price_value"))
            pct = payload.get("clearance_percentage_off")
            pct_str = f" ({pct}% off)" if pct else ""
            lines.append(f"In-store clearance: {cl_price}{pct_str} (online: {online})")
        elif alert_type == "PRICING_ERROR":
            a_price = fmt_price(after.get("price_value"))
            ref = payload.get("reference_price")
            ref_price = fmt_price(ref)
            pct = payload.get("pct_off_reference")
            pct_str = f" ({pct:.0f}% off ref)" if pct else ""
            lines.append(f"Possible pricing error: {a_price} (ref: {ref_price}){pct_str}")

        # Per-store stock info
        if len(store_alerts) > 1:
            stock_parts: list[str] = []
            for sa in sorted(store_alerts, key=lambda x: str(x.get("store_id", ""))):
                sp = sa.get("payload") or {}
                sa_after = sp.get("after", {})
                label, _ = stock_badge(infer_in_stock(sa_after))
                qty = sa_after.get("inventory_qty")
                sid = sa.get("store_id", "?")
                qty_str = f" / {qty} unit{'s' if qty != 1 else ''}" if qty is not None else ""
                stock_parts.append(f"{label}{qty_str} ({sid})")
            lines.append(f"Stock: {', '.join(stock_parts)}")
        else:
            sa = store_alerts[0] if store_alerts else {}
            sp = sa.get("payload") or {}
            sa_after = sp.get("after", {})
            in_stock = infer_in_stock(sa_after)
            qty = sa_after.get("inventory_qty")
            if in_stock is not None:
                label, _ = stock_badge(in_stock)
                qty_str = f" / {qty} unit{'s' if qty != 1 else ''}" if qty is not None else ""
                lines.append(f"Stock: {label}{qty_str}")

    if alert_type == "SPECIAL_BUY":
        lines.append(":clock3: May be a Daily Deal — act fast, could expire in 24h")
    if alert_type == "IN_STORE_CLEARANCE":
        lines.append(":department_store: In-store only — visit store to purchase at clearance price")

    product_url = payload.get("product_url")
    if product_url:
        lines.append(f"<{product_url}|View on HomeDepot.com>")

    return "\n".join(lines)
