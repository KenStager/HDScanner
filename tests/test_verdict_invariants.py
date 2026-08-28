"""Structural honesty invariants for the online deal card.

P0 fixed a class of bug where the card overstated a deal: it struck HD's
asserted list price (never a price we recorded), labelled a real drop "flat",
showed "flat" and "lowest recorded" together, and let HD's claim headline the
flash. These invariants hold that fix in place across a wide matrix of deal
shapes, so the class can never silently return — no matter how the wording of
any individual chip later changes.

Deliberately wording-tolerant: they assert relationships (the struck price is
one WE recorded; the flash never exceeds our measured depth), not exact copy.
"""

from __future__ import annotations

import itertools
import re

from hd.dashboard.components.cards import online_card_html
from hd.dashboard.queries import deal_tier

CAP = 90


def _struck(html: str) -> float | None:
    m = re.search(r'<span class="deal-was">\$([\d,]+\.\d\d)</span>', html)
    return float(m.group(1).replace(",", "")) if m else None


def _chips(html: str) -> list[str]:
    return re.findall(r'<span class="deal-chip[^"]*">(.*?)</span>', html)


def _flash(html: str) -> str:
    m = re.search(r'deal-flash online"><span>.*?</span><span>(.*?)</span>', html)
    return m.group(1) if m else ""


# Each scenario fixes the price-history shape; deal_tier then classifies it the
# way production would, so no impossible tier/field combinations are tested.
_SCENARIOS = {
    # price_varied, low, low_is_older, obs_days, low_is_recent, evidence_outdates_low
    "flat":               (False, None,  False, 12, True,  False),  # never moved
    "new_low":            (True,  100.0, False, 3,  True,  False),  # dropped to today's low
    "sold_cheaper":       (True,  85.0,  True,  8,  True,  False),  # seen below, recently
    "sold_cheaper_old":   (True,  85.0,  True,  8,  False, True),   # old low, fresher evidence
    "sold_cheaper_relic": (True,  85.0,  True,  8,  False, False),  # old low, STALER evidence
}


def _deals():
    """A production-valid matrix: fields fixed by scenario, tier via deal_tier."""
    price = 100.0
    for (scenario, original, high_all, high_window, evidence,
         claimed, is_daily, is_new) in itertools.product(
        list(_SCENARIOS),                    # price-history shape
        [None, 100.0, 150.0, 300.0],         # HD original (some inflated past our high)
        [None, 110.0, 130.0],                # our witnessed all-time high
        [None, 110.0],                       # our windowed high
        [0, 6, 20],                          # our measured depth
        [0, 30, 47],                         # HD's claim
        [False, True],                       # daily strip?
        [False, True],                       # promo <24h?
    ):
        (price_varied, low, low_is_older, obs_days,
         low_is_recent, evidence_outdates_low) = _SCENARIOS[scenario]
        d = {
            "item_id": "1", "title": "M18 Thing", "image_url": None,
            "price": price, "original": original, "claimed_pct": claimed,
            "true_pct": evidence, "witnessed_pct": evidence, "evidence_pct": evidence,
            "high_all": high_all, "high_window": high_window,
            "low_price": low, "low_ts": None, "low_is_older": low_is_older,
            "low_is_recent": low_is_recent,
            "evidence_outdates_low": evidence_outdates_low,
            "price_varied": price_varied,
            "history_days": 4, "obs_days": obs_days,
            "is_new": is_new, "is_daily": is_daily,
        }
        d["tier"] = deal_tier(d)
        yield d


def test_struck_price_is_always_one_we_recorded():
    """The crossed-out price is always a witnessed high of ours, never HD's list."""
    for d in _deals():
        struck = _struck(online_card_html(d, CAP))
        if struck is None:
            continue
        witnessed = {v for v in (d["high_all"], d["high_window"]) if v is not None}
        assert struck in witnessed, (
            f"struck ${struck} is not a witnessed high {witnessed} "
            f"(original={d['original']}, tier={d['tier']})")


def test_flat_and_lowest_never_coexist():
    for d in _deals():
        chips = " | ".join(_chips(online_card_html(d, CAP)))
        assert not ("flat" in chips and "lowest recorded" in chips), chips


def test_flat_never_labels_a_moved_price():
    for d in _deals():
        if not d["price_varied"]:
            continue
        chips = _chips(online_card_html(d, CAP))
        assert not any("flat" in c for c in chips), (d["tier"], chips)


def test_flash_never_overstates_our_measured_depth():
    """A "−N%" flash never claims more than our evidence; HD's number only
    appears as the word "claims"."""
    for d in _deals():
        flash = _flash(online_card_html(d, CAP))
        m = re.match(r"−(\d+)%", flash)
        if m:
            assert int(m.group(1)) <= d["evidence_pct"], (flash, d["evidence_pct"])
        elif flash:
            assert flash.startswith("claims "), flash


def test_new_badge_never_on_the_daily_strip():
    for d in _deals():
        if not d["is_daily"]:
            continue
        assert "NEW" not in _chips(online_card_html(d, CAP))


def test_lower_witnessed_price_never_hidden():
    """Whatever the tier, a lower price we recorded on an earlier day stays
    on the card — as the amber warning when it is recent or out-evidences the
    claim, as a dated context chip when a measured drop verifies over it.
    The tier decides the dress, never the fact's presence."""
    for d in _deals():
        low = d["low_price"]
        if (low is None or not d["low_is_older"] or not d["price_varied"]
                or d["price"] <= low):
            continue
        chips = _chips(online_card_html(d, CAP))
        assert any(c.startswith("seen $") for c in chips), (d["tier"], chips)
