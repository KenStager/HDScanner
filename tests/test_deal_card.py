"""Chip and flash states on the online deal card.

The flash carries OUR number on verified cards and the word "claims" on
unverified ones; the chips carry dated evidence with the span that backs it.
These tests pin the wording so the card can never contradict itself.
"""

from __future__ import annotations

import re
from datetime import datetime

from hd.dashboard.pages.overview import _online_card_html

CAP = 90


def card(**overrides) -> str:
    d = {
        "item_id": "100001", "title": "M18 FUEL Hammer Drill", "image_url": None,
        "price": 49.97, "original": 79.97, "claimed_pct": 38,
        "true_pct": 0, "high_window": None, "history_days": None,
        "special_buy": False, "low_price": None, "low_ts": None,
        "low_is_older": False, "price_varied": False,
        "tier": "unverified", "evidence_pct": 0, "witnessed_pct": 0,
        "high_all": None, "obs_days": None, "is_new": False,
    }
    d.update(overrides)
    return _online_card_html(d, CAP)


def chips(**overrides) -> list[str]:
    return re.findall(r'deal-chip[^"]*">([^<]+)<', card(**overrides))


def flash(**overrides) -> str:
    m = re.search(r'deal-flash online"><span>[^<]*</span><span>([^<]*)<', card(**overrides))
    return m.group(1) if m else ""


class TestFlash:
    def test_verified_flash_headlines_our_depth(self):
        assert flash(tier="verified", evidence_pct=57, witnessed_pct=57) == "−57%"

    def test_unverified_flash_says_claims(self):
        """HD's number never poses as a verdict — the word 'claims' does the work."""
        assert flash() == "claims 38%"

    def test_warned_flash_says_claims(self):
        assert flash(tier="warned", low_price=42.93, price_varied=True,
                     low_is_older=True) == "claims 38%"


class TestVerdictChip:
    def test_no_verdict_renders_nothing(self):
        """Say nothing when we know nothing — a chip on 3 of 4 cards is noise."""
        assert chips() == []

    def test_warned_card_names_the_witnessed_price(self):
        """Must never claim "no price history" while holding a dated low."""
        out = chips(tier="warned", low_price=42.93, low_ts=datetime(2026, 5, 10),
                    low_is_older=True, price_varied=True)
        assert out == ["seen $42.93 · May 10"]

    def test_flat_recent_window(self):
        assert chips(high_window=49.97, history_days=4) == ["flat 4d price"]

    def test_real_discount_names_its_span(self):
        out = chips(tier="verified", true_pct=18, evidence_pct=18,
                    high_window=61.0, history_days=17)
        assert out[0] == "true −18% vs 17d"

    def test_witnessed_low_names_its_watch_span(self):
        """Evidence depth stays visible: 5 watched days must not read like 70.

        The span rides on the measurement chip now — an all-time drop is
        measured against how long we watched, not the recent window."""
        out = chips(tier="verified", price=42.93, low_price=42.93,
                    price_varied=True, witnessed_pct=57, evidence_pct=57,
                    high_all=99.0, obs_days=5, claimed_pct=55)
        assert out == ["true −57% vs 5d", "lowest recorded"]

    def test_our_number_appears_whether_or_not_the_deal_verifies(self):
        """The threshold decides how much a deal is trusted, not which true
        things a card may say. A 9% drop and a 10% drop are told the same way;
        only the tier differs."""
        kw = dict(price=199.97, low_price=199.97, price_varied=True,
                  high_all=219.0, high_window=219.0, history_days=6,
                  obs_days=6, claimed_pct=22)
        under = chips(tier="unverified", true_pct=9, witnessed_pct=9,
                      evidence_pct=9, **kw)
        over = chips(tier="verified", true_pct=10, witnessed_pct=10,
                     evidence_pct=10, **kw)
        assert under[0] == "true −9% vs 6d"
        assert over[0] == "true −10% vs 6d"
        # and each still carries the fact the other used to hide
        assert "lowest recorded" in under and "lowest recorded" in over

    def test_a_shallow_drop_never_reads_as_flat(self):
        """The contradiction that started this: a card cannot say the price
        never moved while striking a higher price it moved from."""
        out = chips(tier="unverified", price=199.97, low_price=199.97,
                    price_varied=True, true_pct=9, witnessed_pct=9,
                    evidence_pct=9, high_all=219.0, high_window=219.0,
                    history_days=6, obs_days=6, claimed_pct=22)
        assert not any("flat" in c for c in out)

    def test_hd_claim_chip_only_on_material_disagreement(self):
        """Claim ≈ evidence is corroboration, not news; a big gap is news."""
        agree = chips(tier="verified", price=42.93, low_price=42.93,
                      price_varied=True, witnessed_pct=57, evidence_pct=57,
                      high_all=99.0, obs_days=5, claimed_pct=55)
        assert not any("HD claims" in c for c in agree)
        disagree = chips(tier="verified", price=42.93, low_price=42.93,
                         price_varied=True, witnessed_pct=20, evidence_pct=20,
                         high_all=53.0, obs_days=5, claimed_pct=53)
        assert "HD claims 53%" in disagree

    def test_fresh_drop_to_a_varied_low_is_shown(self):
        """A price that just hit the low of a real history is news, even today."""
        out = chips(price=42.93, low_price=42.93, low_is_older=False,
                    price_varied=True, high_all=44.0, witnessed_pct=2)
        assert "lowest recorded" in out

    def test_single_observation_low_stays_suppressed(self):
        """An item seen once is at its low by definition — not worth a badge."""
        out = chips(price=42.93, low_price=42.93, price_varied=False)
        assert "lowest recorded" not in out


class TestContextChip:
    """A verified card over an older, lower witnessed price keeps that fact
    visible — same words as the warning, one salience tier down. The tier
    flip must never make the low disappear."""

    def _ctx(self, **o):
        base = dict(tier="verified", price=120.0, true_pct=30, evidence_pct=30,
                    witnessed_pct=0, high_window=171.0, history_days=30,
                    low_price=100.0, low_ts=datetime(2026, 5, 10),
                    low_is_older=True, price_varied=True, obs_days=30,
                    claimed_pct=30)
        base.update(o)
        return base

    def test_verified_card_shows_the_older_low(self):
        assert "seen $100.00 · May 10" in chips(**self._ctx())

    def test_same_words_as_the_warned_chip(self):
        """Same fact, same words, different dress — a wording difference
        between the two salience tiers would be a re-worded verdict."""
        from hd.dashboard.components.cards import verdict_facts
        ctx = verdict_facts(self._ctx(), CAP)
        warned = verdict_facts(self._ctx(tier="warned"), CAP)
        ctx_texts = dict(ctx["chips"])
        warned_texts = dict(warned["chips"])
        seen = [t for t in warned_texts if t.startswith("seen $")]
        assert seen and seen[0] in ctx_texts
        assert warned_texts[seen[0]] == "above"
        assert ctx_texts[seen[0]] == "context"

    def test_caption_carries_the_low_as_a_subordinate_clause(self):
        from hd.dashboard.components.cards import verdict_facts
        cap = verdict_facts(self._ctx(), CAP)["caption"]
        assert cap.startswith("My price record backs the 30% off")
        assert "It has sold lower in my record — $100.00 on May 10." in cap

    def test_never_beside_lowest_recorded(self):
        """price>low and price<=low cannot both hold; the chip slots are
        exclusive by construction — pin it anyway."""
        out = chips(**self._ctx())
        assert "lowest recorded" not in out
        at_low = chips(**self._ctx(price=100.0))
        assert "lowest recorded" in at_low
        assert not any(c.startswith("seen $") for c in at_low)

    def test_undated_low_still_prints_without_a_date(self):
        """Defensive only: production cannot build this shape (low_is_older
        requires a dated low), but a hand-built dict must not hide the fact."""
        assert "seen $100.00" in chips(**self._ctx(low_ts=None))

    def test_full_chip_order_is_pinned(self):
        """Measured band, then the low anchor, then HD's disagreement — the
        eye-span ordering the compact renderers and the band CSS rely on."""
        out = chips(**self._ctx(claimed_pct=45))
        assert out == ["true −30% vs 30d", "seen $100.00 · May 10",
                       "HD claims 45%"]

    def test_watching_span_is_a_calendar_span(self):
        """12 observed days spread over 5 months must read '3mo+', never
        '12d' — a 5-month verdict posing as a 12-day one is the inversion
        of the span promise. obs_days remains the fallback when no
        calendar span is supplied."""
        kw = dict(tier="verified", price=119.0, true_pct=0, witnessed_pct=30,
                  evidence_pct=30, low_price=99.0,
                  low_ts=datetime(2026, 3, 9), low_is_older=True,
                  price_varied=True, high_all=170.0, obs_days=12,
                  claimed_pct=30)
        assert "true −30% vs 3mo+" in chips(watched_days=167, **kw)
        assert "true −30% vs 12d" in chips(**kw)


class TestWasPrice:
    def test_verified_strikes_the_witnessed_high_not_hds_original(self):
        html = card(tier="verified", price=129.0, original=339.0,
                    witnessed_pct=64, evidence_pct=64, high_all=359.0,
                    low_price=129.0, price_varied=True, obs_days=14)
        assert "$359.00" in html
        assert "$339.00" not in html

    def test_unverified_never_strikes_hds_original(self):
        """HD's asserted list is not a price we witnessed; never strike it."""
        assert "$79.97" not in card()

    def test_unverified_strikes_our_witnessed_high(self):
        """A shallow drop still strikes OUR recorded high — not HD's list."""
        html = card(price=749.0, original=1099.0, claimed_pct=32,
                    evidence_pct=6, witnessed_pct=6, true_pct=6,
                    high_all=799.0, high_window=799.0, low_price=749.0,
                    price_varied=True, history_days=4, obs_days=3)
        assert "$799.00" in html
        assert "$1,099.00" not in html


class TestRotation:
    def test_new_deal_carries_the_new_chip(self):
        assert "NEW" in chips(is_new=True)

    def test_old_deal_does_not(self):
        assert "NEW" not in chips(is_new=False)

    def test_new_chip_suppressed_on_the_daily_strip(self):
        """Every daily card is by definition today's deal — NEW says nothing."""
        assert "NEW" not in chips(is_new=True, is_daily=True)


class TestShallowDrop:
    """A real but sub-10% drop to a new low is honest news, not 'flat'."""

    def _shallow(self, **o):
        base = dict(price=749.0, original=1099.0, claimed_pct=32,
                    evidence_pct=6, witnessed_pct=6, true_pct=6,
                    high_all=799.0, high_window=799.0, low_price=749.0,
                    price_varied=True, history_days=4, obs_days=3)
        base.update(o)
        return base

    def test_moved_price_is_not_labelled_flat(self):
        out = chips(**self._shallow())
        assert not any("flat" in c for c in out)

    def test_flat_and_lowest_never_coexist(self):
        out = chips(**self._shallow())
        assert not ("flat 4d price" in out and "lowest recorded" in out)

    def test_shallow_drop_shows_lowest_and_demotes_hd_claim(self):
        out = chips(**self._shallow())
        assert "lowest recorded" in out
        assert "HD claims 32%" in out

    def test_flash_headlines_our_measured_depth_not_hds_claim(self):
        assert flash(**self._shallow()) == "−6%"


class TestChipBudget:
    def test_verified_card_carries_one_evidence_chip(self):
        """One verdict, dated — never a pile of numbers."""
        out = chips(tier="verified", true_pct=18, evidence_pct=18,
                    high_window=61.0, history_days=17, claimed_pct=38)
        assert out == ["true −18% vs 17d", "HD claims 38%"]
        assert not any(c.startswith("high $") for c in out)


class TestCardLink:
    """The card links inward to our own product page, and opens a new window."""

    def test_links_to_the_internal_product_page(self):
        assert 'href="/products/100001"' in card()

    def test_does_not_link_straight_to_homedepot(self):
        assert "homedepot.com" not in card()

    def test_opens_in_a_new_window(self):
        """NiceGUI's sanitizer strips target=, so this is rendered with it off."""
        html = card()
        assert 'target="_blank"' in html
        assert 'rel="noopener"' in html


class TestRecentContext:
    """A card whose big number rests on a months-old witnessed anchor also
    surfaces the recent story — same grammar, each number carrying the
    window it was measured over. (The charger case: −42% vs a March $429,
    while August sat at $279.)"""

    def _stale(self, **o):
        base = dict(tier="verified", price=249.0, true_pct=11, witnessed_pct=42,
                    evidence_pct=42, low_price=249.0, high_all=429.0,
                    high_window=279.0, high_is_recent=False, history_days=8,
                    obs_days=31, watched_days=171, price_varied=True,
                    claimed_pct=42)
        base.update(o)
        return base

    def test_the_recent_number_leads_a_stale_anchor_card(self):
        """A daily surface reports today's event: the band is what a buyer
        saves versus the recent price, and the long, stale-anchored depth
        rides as the quiet dated chip — with HD's claim now printing,
        because the gap between claim and today IS the story."""
        out = chips(**self._stale())
        assert out[0] == "true −11% vs 8d"
        assert "lowest recorded" in out
        assert "true −42% vs 3mo+" in out
        assert "HD claims 42%" in out

    def test_the_struck_price_follows_the_leading_number(self):
        """The struck "was" is the recent window high — the price the reader
        would actually have paid lately, and still a price we recorded."""
        html = card(**self._stale())
        assert '<span class="deal-was">$279.00</span>' in html
        assert "$429.00" not in html

    def test_caption_leads_with_today_and_keeps_the_long_story(self):
        from hd.dashboard.components.cards import verdict_facts
        cap = verdict_facts(self._stale(), CAP)["caption"]
        assert cap.startswith("Down 11% from the $279.00 I've tracked "
                              "over the last 8d")
        assert "my lowest recorded in 3mo+ of tracking" in cap
        assert "I've seen it as high as $429.00" in cap

    def test_the_flash_follows_the_leading_number(self):
        assert flash(**self._stale()) == "−11%"

    def test_fresh_anchor_keeps_the_strongest_number(self):
        out = chips(**self._stale(high_is_recent=True))
        assert out[0] == "true −42% vs 3mo+"
        assert sum(c.startswith("true −") for c in out) == 1

    def test_absent_recency_info_changes_nothing(self):
        d = self._stale()
        d.pop("high_is_recent")
        out = chips(**d)
        assert out[0] == "true −42% vs 3mo+"
        assert sum(c.startswith("true −") for c in out) == 1

    def test_a_small_gap_stays_single_number(self):
        """Within 5 points the two windows tell the same story."""
        out = chips(**self._stale(true_pct=38))
        assert out[0] == "true −42% vs 3mo+"
        assert sum(c.startswith("true −") for c in out) == 1

    def test_the_long_number_never_wears_the_band(self):
        from hd.dashboard.components.cards import verdict_facts
        vf = verdict_facts(self._stale(), CAP)
        assert [c for t, c in vf["chips"] if t == "true −42% vs 3mo+"] == [""]

    def test_ordering_follows_the_displayed_number(self):
        """Position and bands must agree: a divergent card banding −11% may
        not outrank a fresh −36%, whatever its corroborated depth."""
        from hd.dashboard.components.cards import headline_pct
        divergent = self._stale()
        fresh = dict(tier="verified", price=99.0, true_pct=36,
                     witnessed_pct=36, evidence_pct=36, high_window=153.94,
                     high_all=153.94, high_is_recent=True, history_days=5,
                     obs_days=5, low_price=99.0, price_varied=True)
        assert headline_pct(divergent) == 11
        assert headline_pct(fresh) == 36


class TestStaleOnlyAnchor:
    """No recent drop at all (the deal price predates our window): nothing
    bands, nothing strikes; "lowest recorded" leads, dated with how long it
    has held, and the historical depth rides as the quiet chip."""

    def _stale0(self, **o):
        base = dict(tier="verified", price=249.0, true_pct=0, witnessed_pct=33,
                    evidence_pct=33, low_price=249.0, high_all=369.0,
                    high_window=249.0, high_is_recent=False, history_days=8,
                    obs_days=31, watched_days=171, low_age_days=115,
                    low_ts=datetime(2026, 5, 1), price_varied=True,
                    claimed_pct=33)
        base.update(o)
        return base

    def test_lowest_recorded_leads_with_its_span(self):
        out = chips(**self._stale0())
        assert out[0] == "lowest recorded · 3mo+"
        assert "true −33% vs 3mo+" in out

    def test_nothing_bands_and_nothing_strikes(self):
        from hd.dashboard.components.cards import verdict_facts
        vf = verdict_facts(self._stale0(), CAP)
        assert vf["flash"] == ""
        assert vf["struck"] is None
        assert all(cls != "true" for _, cls in vf["chips"])

    def test_caption_is_dated_and_unchanged_honest(self):
        from hd.dashboard.components.cards import verdict_facts
        cap = verdict_facts(self._stale0(), CAP)["caption"]
        assert cap.startswith("My lowest recorded price since May 1")
        assert "33% under the $369.00 I've seen in 3mo+ of tracking" in cap
        assert "unchanged over the last 8d" in cap

    def test_ranks_at_the_bottom(self):
        from hd.dashboard.components.cards import headline_pct
        assert headline_pct(self._stale0()) == 0

    def test_not_at_lowest_variant_keeps_its_anchor_chip(self):
        out = chips(**self._stale0(price=229.0, low_price=209.0,
                                   low_is_older=True, low_is_recent=False,
                                   evidence_outdates_low=True))
        assert out[0].startswith("seen $209.00")
        assert "true −33% vs 3mo+" in out
        assert all(not c.startswith("true −0") for c in out)
