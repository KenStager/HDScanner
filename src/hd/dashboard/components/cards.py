"""Deal card HTML builders.

Deliberately free of NiceGUI imports so any renderer can produce the same cards
the dashboard shows — one card language, one verdict vocabulary, wherever a deal
is rendered.
"""

from __future__ import annotations

import html as _html

from hd.dashboard.components.formatters import fmt_history_span, fmt_low_date


def online_card_html(d: dict, cap_days: int) -> str:
    """Online deal shelf tag.

    The flash carries OUR number: a verified card headlines the depth our
    record measured, and HD's claim is demoted to the word "claims" (on
    unverified cards) or a small disagreement chip (when their number and
    ours part ways). The chips carry the evidence — dated, with the span
    that backs it — so a 3-day verdict can never pose as a 30-day one.
    """
    title = _html.escape(d["title"])
    url = f'/products/{_html.escape(str(d["item_id"]), quote=True)}'

    if d.get("image_url"):
        img = f'<img src="{_html.escape(d["image_url"], quote=True)}" alt="" loading="lazy">'
    else:
        img = '<div class="placeholder">🔧</div>'

    price = f"${d['price']:,.2f}"
    tier = d.get("tier", "unverified")
    claimed = int(d["claimed_pct"] or 0)
    true_pct = int(d["true_pct"] or 0)
    witnessed = int(d.get("witnessed_pct") or 0)
    evidence = int(d.get("evidence_pct") or 0)
    if d.get("is_daily"):
        label = "Daily Deal"
    elif d.get("special_buy"):
        label = "Special Buy"
    else:
        label = "Online Deal"

    if tier == "verified":
        flash_pct = f"−{evidence}%"
        # The struck price is one our own record saw it sell for, never HD's
        # asserted original.
        was_val = d.get("high_all") if witnessed >= true_pct else d.get("high_window")
    else:
        flash_pct = f"claims {claimed}%" if claimed else ""
        was_val = d.get("original")

    was = (
        f'<span class="deal-was">${was_val:,.2f}</span>'
        if was_val and was_val > d["price"]
        else ""
    )

    chips = ""
    if d.get("is_new"):
        chips += '<span class="deal-chip new">NEW</span>'
    # No snapshot has ever shown this item buyable — HD lists it but returns
    # no fulfillment data. The deal may be real; the reader deserves the doubt.
    if d.get("availability_unknown"):
        chips += '<span class="deal-chip">availability unknown</span>'

    span = fmt_history_span(d.get("history_days"), cap_days)
    low = d.get("low_price")
    if tier == "verified":
        if true_pct >= witnessed:
            chips += f'<span class="deal-chip true">true −{true_pct}% vs {span}</span>'
        elif low is not None and d["price"] <= low:
            watched = fmt_history_span(d.get("obs_days"), cap_days)
            chips += f'<span class="deal-chip best">lowest recorded · {watched}</span>'
        elif d.get("high_all") is not None:
            watched = fmt_history_span(d.get("obs_days"), cap_days)
            chips += (f'<span class="deal-chip">high ${d["high_all"]:,.0f}'
                      f' · {watched}</span>')
        # HD's number gets a chip only when it materially disagrees with ours
        if claimed and abs(claimed - evidence) > 5:
            chips += f'<span class="deal-chip">HD claims {claimed}%</span>'
    elif tier == "warned":
        when = fmt_low_date(d.get("low_ts"))
        chips += (f'<span class="deal-chip above">seen ${low:,.2f}'
                  f'{" · " + when if when else ""}</span>')
    else:
        # Unverified: the record is too young to speak. Say what little we
        # know, or nothing — a chip on 3 of every 4 cards is noise.
        if d.get("high_window") is not None and true_pct < 10:
            chips += f'<span class="deal-chip flat">flat {span} price</span>'
        if low is not None and d.get("price_varied") and d["price"] <= low:
            chips += '<span class="deal-chip best">lowest recorded</span>'

    return (
        f'<a class="deal-card" href="{url}" target="_blank" rel="noopener">'
        f'<div class="deal-img">{img}</div>'
        f'<div class="deal-flash online"><span>{label}</span><span>{flash_pct}</span></div>'
        f'<div class="deal-price-row"><span class="deal-price">{price}</span>{was}</div>'
        f'<div class="deal-title">{title}</div>'
        f'<div class="deal-foot">{chips}</div>'
        f'</a>'
    )
