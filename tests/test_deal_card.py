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
        """Evidence depth stays visible: 5 watched days must not read like 70."""
        out = chips(tier="verified", price=42.93, low_price=42.93,
                    price_varied=True, witnessed_pct=57, evidence_pct=57,
                    high_all=99.0, obs_days=5, claimed_pct=55)
        assert out == ["lowest recorded · 5d"]

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


class TestWasPrice:
    def test_verified_strikes_the_witnessed_high_not_hds_original(self):
        html = card(tier="verified", price=129.0, original=339.0,
                    witnessed_pct=64, evidence_pct=64, high_all=359.0,
                    low_price=129.0, price_varied=True, obs_days=14)
        assert "$359.00" in html
        assert "$339.00" not in html

    def test_unverified_falls_back_to_hds_original(self):
        assert "$79.97" in card()


class TestRotation:
    def test_new_deal_carries_the_new_chip(self):
        assert "NEW" in chips(is_new=True)

    def test_old_deal_does_not(self):
        assert "NEW" not in chips(is_new=False)


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
