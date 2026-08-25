"""Deal card HTML builders.

Deliberately free of NiceGUI imports so any renderer can produce the same cards
the dashboard shows — one card language, one verdict vocabulary, wherever a deal
is rendered.
"""

from __future__ import annotations

import html as _html

from hd.dashboard.components.formatters import fmt_history_span, fmt_low_date


def _anchor_mode(d: dict) -> str | None:
    """Does the strongest measured depth rest on a witnessed anchor older
    than the recency bound, while the recent window says otherwise? A daily
    surface reports today's event, and leading with the largest defensible
    reference — corroborated or not — is the anchor-inflation move this
    project exists to call out.

    "recent" — the window measured a real drop: THAT number leads, the long
    depth demotes to a quiet dated chip. "stale" — the window measured NO
    drop at all (the deal price predates it): nothing leads as a band;
    "lowest recorded" carries the card, dated with how long it has held.
    None — no divergence (fresh anchor, convergent windows, or an older
    caller without high_is_recent): the pre-existing behavior."""
    true_pct = int(d.get("true_pct") or 0)
    witnessed = int(d.get("witnessed_pct") or 0)
    if not (
        witnessed > true_pct and d.get("high_is_recent") is False
        and d.get("high_window") is not None
        and d.get("history_days") is not None
        and witnessed - true_pct > 5
    ):
        return None
    return "recent" if true_pct >= 1 else "stale"


def headline_pct(d: dict) -> int:
    """The measured number the card LEADS with (0 = no band: nothing moved
    recently). Ordering surfaces rank by this same number, so position and
    legible bands always agree; trust (deal_tier, alerting, selection bars)
    stays on the full evidence — the threshold decides how far a deal is
    trusted, never which number it may say."""
    mode = _anchor_mode(d)
    if mode == "recent":
        return int(d.get("true_pct") or 0)
    if mode == "stale":
        return 0
    return int(d.get("evidence_pct") or 0)


def verdict_facts(d: dict, cap_days: int) -> dict:
    """Single source of truth for a deal's verdict across every surface.

    Every rendering of a deal — the card (flash, struck price, chips) and
    the longer summary line — reads from here, so two renderings can never
    describe the same item differently: the failure that let one say "flat"
    while the other said "too new" for the same deal.

    Two honesty invariants hold for every tier:
      * The struck-through price is always a price WE recorded (a witnessed
        high), never HD's asserted original. When we never recorded a higher
        price, nothing is struck.
      * The flash headlines OUR measured depth when we have one; only when we
        have measured no drop at all does HD's number appear, and then as a
        "claims" — never posing as our verdict.

    Returns {flash, struck, chips, caption} where chips is a list of
    (label, css_class) pairs.
    """
    price = float(d["price"])
    claimed = int(d.get("claimed_pct") or 0)
    true_pct = int(d.get("true_pct") or 0)
    witnessed = int(d.get("witnessed_pct") or 0)
    evidence = int(d.get("evidence_pct") or 0)
    tier = d.get("tier", "unverified")
    low = d.get("low_price")
    original = d.get("original")
    history_days = d.get("history_days")
    obs_days = d.get("obs_days")
    price_varied = bool(d.get("price_varied"))
    span = fmt_history_span(history_days, cap_days)
    # The watching span is a CALENDAR span (first observation → now), not the
    # count of distinct observed days: 12 observed days spread over 5 months
    # must read "3mo+", never "12d" — a 5-month verdict posing as a 12-day
    # one is the exact inversion of the promise two lines down. obs_days
    # remains the fallback for callers that predate watched_days.
    watched_days = d.get("watched_days")
    if watched_days is None:
        watched_days = obs_days
    watched = fmt_history_span(watched_days, cap_days)

    # Our own witnessed ceiling — the most we ever recorded this selling for.
    our_high = d.get("high_all")
    if our_high is None:
        our_high = d.get("high_window")
    has_drop = our_high is not None and our_high > price

    chips: list[tuple[str, str]] = []

    # The measurement chip: our own number and the span it was measured over,
    # in one grammar on every tier that has one. It leads, and the facts that
    # used to replace it — "lowest recorded", HD's claim — now sit beside it
    # as extra badges.
    #
    # It used to be reserved for the verified tier, which meant a 10% drop read
    # "true −10% vs 6d" and a 9% drop read "lowest recorded" with no number at
    # all. One percentage point either side of VERIFIED_MIN_PCT swapped the
    # entire vocabulary, and each vocabulary hid a fact the other showed. The
    # threshold decides how much we trust a deal, and so what we rank and alert
    # on; it has no business deciding which true things a card is allowed to
    # say. "true" means measured by us rather than claimed by HD, which is
    # exactly as accurate at 9% as at 17%.
    #
    # The span follows whichever measurement produced the number — a window
    # drop reads "vs 6d", an all-time drop reads against however long we have
    # watched — so a 3-day verdict can never pose as a 30-day one.
    # Which number leads (see _stale_anchor_divergence / headline_pct). When
    # the swap fires, the recent measurement is the band and the long,
    # stale-anchored one becomes the quiet dated chip — same grammar, each
    # number carrying the window it was measured over.
    mode = _anchor_mode(d)
    divergent = mode == "recent"
    stale_only = mode == "stale"
    if mode is not None:
        display_pct = true_pct  # 0 in the stale case: no band at all
        ev_span = span
        long_context = (f"true −{witnessed}% vs {watched}", "")
    else:
        display_pct = evidence
        ev_span = span if true_pct >= witnessed else watched
        long_context = None
    measured = (f"true −{display_pct}% vs {ev_span}", "true")
    at_lowest = low is not None and price <= low

    if tier == "verified":
        # The struck price follows the leading measurement's own anchor —
        # always a price WE recorded, and on a divergent card the recent
        # window high: the price the reader would actually have paid lately.
        # A stale-only card strikes nothing and bands nothing: nothing moved
        # recently, and the historical depth lives in the quiet chip and the
        # caption instead of dressing up as today's news.
        if stale_only:
            flash = ""
            struck = None
        elif divergent:
            flash = f"−{display_pct}%"
            struck = d.get("high_window")
        else:
            flash = f"−{display_pct}%"
            struck = d.get("high_all") if witnessed >= true_pct else d.get("high_window")
        # An older, lower price we once recorded. deal_tier only lets a card
        # verify over such a low when the low is stale and the drop is
        # measured, but the fact itself must never disappear with the tier:
        # it prints here as dated context, in the exact words the warning
        # uses, one salience tier down. Same fact, same words, quieter dress.
        sold_lower = (
            low is not None and price > low and price_varied
            and bool(d.get("low_is_older"))
        )
        if evidence > 0 and not stale_only:
            chips.append(measured)
        if at_lowest:
            held = None
            if stale_only and d.get("low_age_days") is not None:
                held = fmt_history_span(d["low_age_days"], cap_days)
            chips.append(
                (f"lowest recorded · {held}" if held else "lowest recorded",
                 "best"))
        elif sold_lower:
            when = fmt_low_date(d.get("low_ts"))
            chips.append(
                (f"seen ${low:,.2f}{' · ' + when if when else ''}", "context"))
        if long_context:
            chips.append(long_context)
        if stale_only:
            tail = (f"{witnessed}% under the ${our_high:,.2f} I've seen in "
                    f"{watched} of tracking, unchanged over the last {span}.")
            if at_lowest:
                when_low = fmt_low_date(d.get("low_ts"))
                caption = (f"My lowest recorded price"
                           f"{' since ' + when_low if when_low else ''} — "
                           + tail)
            else:
                caption = tail[0].upper() + tail[1:]
        elif divergent:
            hw = float(d["high_window"])
            caption = (f"Down {display_pct}% from the ${hw:,.2f} I've tracked "
                       f"over the last {span}")
            if at_lowest:
                caption += (f" — my lowest recorded in {watched} of tracking. "
                            f"I've seen it as high as ${our_high:,.2f} in "
                            f"that time.")
            else:
                caption += (f". I've seen it as high as ${our_high:,.2f} in "
                            f"{watched} of tracking.")
        elif true_pct >= witnessed:
            caption = (f"My price record backs the {evidence}% off — down from "
                       f"what I tracked over {span}.")
        elif at_lowest:
            caption = (f"Real {evidence}% off — the lowest price I've recorded "
                       f"in {watched} of tracking.")
        else:
            caption = f"My record supports about {evidence}% off."
        if sold_lower:
            when = fmt_low_date(d.get("low_ts"))
            caption += (f" It has sold lower in my record — ${low:,.2f}"
                        f"{' on ' + when if when else ''}.")
        # HD's number gets a chip when it materially disagrees with the
        # number the card leads with — on a divergent card that gap is the
        # story: the claim was real once, the recent price says today. A
        # band-less stale card compares against its one number, the
        # historical depth.
        claim_ref = witnessed if stale_only else display_pct
        if claimed and abs(claimed - claim_ref) > 5:
            chips.append((f"HD claims {claimed}%", ""))
        return {"flash": flash, "struck": struck, "chips": chips, "caption": caption}

    # ---- everything below is non-verified: our number leads, HD's claim
    # never poses as ours, and nothing of HD's is struck through. ----
    flash = (f"−{display_pct}%" if display_pct > 0
             else (f"claims {claimed}%" if claimed else ""))
    if divergent:
        struck = d.get("high_window")
    else:
        struck = our_high if has_drop else None

    if tier == "warned":
        when = fmt_low_date(d.get("low_ts"))
        if low is not None:
            chips.append(
                (f"seen ${low:,.2f}{' · ' + when if when else ''}", "above"))
        claim = f"HD claims {claimed}% off" if claimed else "Listed as a deal"
        seen = f"${low:,.2f}" if low is not None else "less"
        caption = (f"{claim}, but my tracker saw it selling for {seen}"
                   f"{' on ' + when if when else ''}.")
        return {"flash": flash, "struck": struck, "chips": chips, "caption": caption}

    if tier == "hollow":
        if d.get("high_window") is not None:
            chips.append((f"flat {span} price", "flat"))
        caption = (f"HD claims {claimed}% off, but the price hasn't moved in "
                   f"{watched} of my watching.")
        return {"flash": flash, "struck": struck, "chips": chips, "caption": caption}

    # ---- unverified ----
    is_new_low = price_varied and low is not None and price <= low and has_drop
    inflated = original is not None and our_high is not None and original > our_high

    if is_new_low:
        # A real, if shallow, drop to a new low. Say exactly that — never
        # "flat", which contradicts a just-set low — and say how deep, which
        # this branch used to leave off the card entirely.
        if evidence > 0:
            chips.append(measured)
        chips.append(("lowest recorded", "best"))
        if long_context:
            chips.append(long_context)
        if claimed and abs(claimed - display_pct) > 5:
            chips.append((f"HD claims {claimed}%", ""))
        if divergent:
            caption = (f"Down to my lowest recorded ${price:,.2f} — "
                       f"{display_pct}% under the "
                       f"${float(d['high_window']):,.2f} I've tracked over "
                       f"the last {span}. I've seen it as high as "
                       f"${our_high:,.2f}.")
        elif inflated and claimed - evidence > 5:
            caption = (f"Down to my lowest recorded ${price:,.2f} — about "
                       f"{evidence}% under the ${our_high:,.2f} I've tracked. "
                       f"HD claims {claimed}% off ${original:,.2f}, a price I've "
                       f"never recorded.")
        else:
            caption = (f"Down to my lowest recorded ${price:,.2f} — about "
                       f"{evidence}% under the ${our_high:,.2f} I've tracked.")
        return {"flash": flash, "struck": struck, "chips": chips, "caption": caption}

    if not price_varied and d.get("high_window") is not None:
        # Genuinely flat: the price never moved while we watched. Only here
        # does "flat" tell the truth.
        chips.append((f"flat {span} price", "flat"))
        caption = (f"HD claims {claimed}% off, but I've only ever seen it at "
                   f"${price:,.2f} in {watched} of watching." if claimed
                   else "No discount claimed or measured — listed in today's set as-is.")
        return {"flash": flash, "struck": struck, "chips": chips, "caption": caption}

    # Nothing witnessed to weigh yet. Reserve "too new" for records that really
    # are too young; an older record we simply can't corroborate says so plainly.
    if not claimed:
        caption = "No discount claimed or measured — listed in today's set as-is."
    elif history_days is None:
        caption = f"HD claims {claimed}% off — too new in my record to verify yet."
    else:
        caption = f"HD claims {claimed}% off — more than my record can back yet."
    return {"flash": flash, "struck": struck, "chips": chips, "caption": caption}


def online_card_html(d: dict, cap_days: int) -> str:
    """Online deal shelf tag.

    The flash carries OUR number: a card headlines the depth our record
    measured, and HD's claim is demoted to the word "claims" (when we measured
    no drop) or a small disagreement chip. The chips carry the evidence —
    dated, with the span that backs it — so a 3-day verdict can never pose as a
    30-day one. All verdict text comes from verdict_facts, so every rendering
    of this deal agrees.
    """
    title = _html.escape(d["title"])
    url = f'/products/{_html.escape(str(d["item_id"]), quote=True)}'

    if d.get("image_url"):
        img = f'<img src="{_html.escape(d["image_url"], quote=True)}" alt="" loading="lazy">'
    else:
        img = '<div class="placeholder">🔧</div>'

    price = f"${d['price']:,.2f}"
    if d.get("is_daily"):
        label = "Daily Deal"
    elif d.get("special_buy"):
        label = "Special Buy"
    else:
        label = "Online Deal"

    vf = verdict_facts(d, cap_days)
    struck = vf["struck"]
    was = (
        f'<span class="deal-was">${struck:,.2f}</span>'
        if struck and struck > d["price"]
        else ""
    )

    chips = ""
    # NEW marks a deal whose promo we first saw <24h ago — a "fresh find" signal
    # for the always-on online board. On the daily strip every card is by
    # definition today's deal, so the badge says nothing there and is suppressed.
    if d.get("is_new") and not d.get("is_daily"):
        chips += '<span class="deal-chip new">NEW</span>'
    # No snapshot has ever shown this item buyable — HD lists it but returns no
    # fulfillment data. The deal may be real; the reader deserves the doubt.
    if d.get("availability_unknown"):
        chips += '<span class="deal-chip">availability unknown</span>'
    for label_text, cls in vf["chips"]:
        cls_attr = f" {cls}" if cls else ""
        chips += f'<span class="deal-chip{cls_attr}">{label_text}</span>'

    return (
        f'<a class="deal-card" href="{url}" target="_blank" rel="noopener">'
        f'<div class="deal-img">{img}</div>'
        f'<div class="deal-flash online"><span>{label}</span><span>{vf["flash"]}</span></div>'
        f'<div class="deal-price-row"><span class="deal-price">{price}</span>{was}</div>'
        f'<div class="deal-title">{title}</div>'
        f'<div class="deal-foot">{chips}</div>'
        f'</a>'
    )
