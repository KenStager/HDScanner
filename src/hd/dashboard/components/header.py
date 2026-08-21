"""Shared navigation header and visual theme for all dashboard pages.

The theme leans on the vernacular of the hunt itself: a dark warehouse-wall
ground, Home Depot orange as the single accent rule, and the yellow in-store
clearance tag reserved for deal cards — the one loud element on the page.
"""

from __future__ import annotations

from nicegui import ui

_NAV_LINKS = [
    ("Deals", "/"),
    ("Products", "/products"),
    ("Alerts", "/alerts"),
    ("Stores", "/stores"),
]

_THEME = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --hd-bg: #1B1C1E;
  --hd-surface: #242629;
  --hd-orange: #F96302;
  --hd-yellow: #FFD100;
  --hd-red: #DB021D;
  --hd-text: #ECEDEE;
  --hd-muted: #9BA0A6;
}
body { background: var(--hd-bg) !important; }
.hd-display { font-family: 'Barlow Condensed', sans-serif; }
body, .hd-body { font-family: 'Inter', sans-serif; }

.hd-header {
  background: var(--hd-bg) !important;
  border-bottom: 3px solid var(--hd-orange);
  box-shadow: none !important;
}
.hd-wordmark {
  font-family: 'Barlow Condensed', sans-serif;
  font-weight: 700; font-size: 1.5rem; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--hd-text);
}
.hd-wordmark .accent { color: var(--hd-orange); }
.hd-nav a {
  font-family: 'Inter', sans-serif; font-size: 0.85rem; font-weight: 500;
  color: var(--hd-muted); text-decoration: none; padding-bottom: 2px;
}
.hd-nav a:hover { color: var(--hd-text); }
.hd-nav a.active { color: var(--hd-text); border-bottom: 2px solid var(--hd-orange); }

/* status line */
.hd-status { color: var(--hd-muted); font-size: 0.8rem; }
.hd-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.hd-dot.ok { background: #3fb950; }
.hd-dot.stale { background: var(--hd-yellow); }
.hd-dot.degraded { background: var(--hd-red); }

/* the scanner's own condition: absent when fine, unmissable when not */
.hd-banner {
  margin: 12px 24px 0 24px; padding: 10px 14px 12px 14px; border-radius: 6px;
  background: var(--hd-surface); border-left: 4px solid var(--hd-yellow);
}
.hd-banner.fail { border-left-color: var(--hd-red); }
.hd-banner-mark {
  width: 9px; height: 9px; border-radius: 50%;
  display: inline-block; background: var(--hd-yellow);
}
.hd-banner.fail .hd-banner-mark { background: var(--hd-red); }
.hd-banner-head { font-weight: 600; font-size: 0.9rem; color: var(--hd-text); }
.hd-banner-line {
  color: var(--hd-muted); font-size: 0.82rem;
  margin-left: 21px; line-height: 1.5;
}
.hd-banner-line.muted { opacity: 0.7; }

/* warnings: findable without being an alarm. Several are permanent by choice,
   and a bar that is always lit is one nobody reads on the day it matters. */
.hd-advisory {
  margin: 8px 24px 0 24px;
  color: var(--hd-muted); font-size: 0.8rem;
}
.hd-advisory .q-expansion-item__container > .q-item {
  min-height: 0; padding: 2px 0; color: var(--hd-muted);
}
.hd-advisory .q-item__label { font-size: 0.8rem; }

/* deal grid + shelf-tag cards */
.deal-grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
}
a.deal-card {
  display: flex; flex-direction: column; text-decoration: none;
  background: #FFFFFF; border-radius: 6px; overflow: hidden;
  transition: transform 0.12s ease, box-shadow 0.12s ease;
  box-shadow: 0 1px 3px rgba(0,0,0,0.4);
}
a.deal-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.5); }
a.deal-card:focus-visible { outline: 3px solid var(--hd-orange); outline-offset: 2px; }
.deal-img {
  aspect-ratio: 1 / 1; background: #FFFFFF; display: flex;
  align-items: center; justify-content: center; position: relative;
}
.deal-img img { max-width: 88%; max-height: 88%; object-fit: contain; }
.deal-img .placeholder { color: #C9CCD1; font-size: 3rem; }
.deal-flash {
  background: var(--hd-yellow); color: #111;
  font-family: 'Barlow Condensed', sans-serif; font-weight: 700;
  font-size: 0.95rem; letter-spacing: 0.08em; text-transform: uppercase;
  padding: 3px 10px; display: flex; justify-content: space-between; align-items: center;
}
.deal-flash.hot { background: var(--hd-red); color: #fff; }
.deal-price-row { display: flex; align-items: baseline; gap: 8px; padding: 6px 10px 0 10px; }
.deal-price {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 700;
  font-size: 1.9rem; color: #111; line-height: 1;
}
.deal-was { color: #8a8f95; font-size: 0.85rem; text-decoration: line-through; }
.deal-title {
  color: #3a3d40; font-size: 0.78rem; line-height: 1.35;
  padding: 0 10px; margin: 4px 0 6px 0;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; height: 2.7em;
}
.deal-foot {
  display: flex; gap: 6px; align-items: center; padding: 0 10px 10px 10px;
  /* wrap rather than clip: nowrap+hidden amputated the anchor chip, which
     renders last and carries the evidence */
  flex-wrap: wrap; row-gap: 4px; min-height: 30px;
}
.deal-chip { white-space: nowrap; }
.deal-chip {
  font-size: 0.7rem; font-weight: 600; border-radius: 3px; padding: 2px 7px;
  background: #EEF0F2; color: #3a3d40;
}
.deal-chip.low { background: var(--hd-red); color: #fff; }
.deal-chip.new { background: var(--hd-orange); color: #fff; }
.deal-chip.true { background: #1a7f37; color: #fff; }
.deal-chip.flat { background: #FFF3BF; color: #6b5d00; }
/* durable price anchors: witnessed best price vs paying above a witnessed one */
.deal-chip.best { background: #0b6bcb; color: #fff; }
.deal-chip.above { background: var(--hd-red); color: #fff; }

/* online deal flash */
.deal-flash.online { background: var(--hd-orange); color: #fff; }

/* card wrapper + hide/restore control */
.deal-wrap { position: relative; }
.deal-wrap.dimmed a.deal-card { opacity: 0.45; filter: grayscale(0.7); }
.deal-wrap .deal-hide {
  position: absolute; top: 6px; right: 6px; z-index: 2;
  background: rgba(27,28,30,0.75) !important; color: #fff !important;
  opacity: 0; transition: opacity 0.12s ease;
}
.deal-wrap:hover .deal-hide, .deal-wrap .deal-hide:focus-visible { opacity: 1; }
.deal-wrap.dimmed .deal-hide { opacity: 1; }

/* filter chips */
.hd-chip {
  font-family: 'Inter', sans-serif; font-size: 0.8rem; font-weight: 600;
  border: 1px solid #3a3d42; border-radius: 999px; padding: 4px 14px;
  color: var(--hd-muted); background: transparent; cursor: pointer;
}
.hd-chip:hover { color: var(--hd-text); border-color: var(--hd-muted); }
.hd-chip.active { background: var(--hd-orange); border-color: var(--hd-orange); color: #fff; }

/* store tabs */
.hd-storetab {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 600; font-size: 1.15rem;
  letter-spacing: 0.05em; text-transform: uppercase; color: var(--hd-muted);
  padding: 6px 2px; cursor: pointer; border-bottom: 3px solid transparent;
  background: transparent; border-top: none; border-left: none; border-right: none;
}
.hd-storetab.active { color: var(--hd-text); border-bottom-color: var(--hd-orange); }
.hd-storetab .count { color: var(--hd-orange); }

.hd-section-label {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 600; font-size: 0.95rem;
  letter-spacing: 0.12em; text-transform: uppercase; color: var(--hd-muted);
}

/* Today's Daily Deals: a pinned panel on its own surface, visibly apart from
   the ranked grid below it */
.hd-daily-panel {
  background: var(--hd-surface);
  border: 1px solid #33363B;
  border-top: 3px solid var(--hd-orange);
  border-radius: 10px;
  padding: 14px 16px 16px 16px;
}
.hd-daily-panel .hd-section-label { color: var(--hd-text); }

/* ---- product detail: the item's dossier ---- */
.pd-wrap { max-width: 1160px; margin: 0 auto; width: 100%; padding: 0 24px; }
.pd-back {
  font-size: 0.8rem; font-weight: 500; color: var(--hd-muted);
  text-decoration: none;
}
.pd-back:hover { color: var(--hd-text); }

.pd-identity { display: flex; gap: 20px; align-items: flex-start; margin-top: 14px; }
.pd-img {
  width: 108px; height: 108px; background: #FFFFFF; border-radius: 6px;
  flex-shrink: 0; display: flex; align-items: center; justify-content: center;
}
.pd-img img { max-width: 86%; max-height: 86%; object-fit: contain; }
.pd-img .placeholder { color: #C9CCD1; font-size: 2.4rem; }
.pd-title {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 700;
  font-size: 1.85rem; line-height: 1.12; color: var(--hd-text); margin: 0;
}
.pd-meta {
  display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 8px;
  color: var(--hd-muted); font-size: 0.82rem;
}
.pd-meta a { color: var(--hd-orange); text-decoration: none; }
.pd-meta a:hover { text-decoration: underline; }

/* store verdict cards */
.pd-stores {
  display: grid; gap: 16px; margin-top: 20px;
  grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
}
.pd-store { background: var(--hd-surface); border-radius: 8px; overflow: hidden; }
.pd-store-head {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: 12px 16px 0 16px;
}
.pd-store-name {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 600;
  font-size: 1.2rem; letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--hd-text); display: flex; align-items: center; gap: 8px;
}
.pd-store-scan { color: var(--hd-muted); font-size: 0.75rem; }
.pd-flash {
  margin-top: 10px; padding: 4px 16px;
  background: var(--hd-yellow); color: #111;
  font-family: 'Barlow Condensed', sans-serif; font-weight: 700;
  font-size: 1rem; letter-spacing: 0.08em; text-transform: uppercase;
  display: flex; justify-content: space-between; align-items: center;
}
.pd-flash.hot { background: var(--hd-red); color: #fff; }
.pd-flash.online { background: var(--hd-orange); color: #fff; }
.pd-price-row {
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  padding: 12px 16px 0 16px;
}
.pd-price {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 700;
  font-size: 2.5rem; line-height: 1; color: var(--hd-text);
}
.pd-was { color: var(--hd-muted); font-size: 0.95rem; text-decoration: line-through; }
.pd-stock {
  display: flex; align-items: center; gap: 8px; padding: 8px 16px 0 16px;
  color: var(--hd-muted); font-size: 0.85rem;
}
.pd-store-links { padding: 12px 16px 14px 16px; font-size: 0.8rem; line-height: 1.6; }
.pd-store-links a { color: var(--hd-muted); text-decoration: underline; }
.pd-store-links a:hover { color: var(--hd-text); }
.pd-store-links .hint { color: var(--hd-muted); opacity: 0.7; font-size: 0.72rem; }

/* store hue dots — same assignment as the chart series */
.pd-dot { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }

/* price record — witnessed facts from item_price_stats */
.pd-record { background: var(--hd-surface); border-radius: 8px; padding: 4px 16px; }
.pd-record-row {
  display: flex; align-items: baseline; gap: 12px 28px; flex-wrap: wrap;
  padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.06);
}
.pd-record-row:last-child { border-bottom: none; }
.pd-record-store {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 600;
  font-size: 1.05rem; letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--hd-text); min-width: 110px;
  display: flex; align-items: center; gap: 8px;
}
.pd-fact { display: flex; align-items: baseline; gap: 7px; }
.pd-fact .k {
  color: var(--hd-muted); font-size: 0.72rem; font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase;
}
.pd-fact .v {
  font-family: 'Barlow Condensed', sans-serif; font-weight: 700;
  font-size: 1.25rem; color: var(--hd-text);
}
.pd-fact .d { color: var(--hd-muted); font-size: 0.8rem; }
.pd-record-note { color: var(--hd-muted); font-size: 0.85rem; }

/* chart + section panels */
.pd-section { margin-top: 26px; }
.pd-panel { background: var(--hd-surface); border-radius: 8px; padding: 14px 16px 8px 16px; margin-top: 10px; }

/* activity feed */
.pd-feed { background: var(--hd-surface); border-radius: 8px; padding: 4px 16px; margin-top: 10px; }
.pd-feed-row {
  display: flex; gap: 14px; align-items: baseline; padding: 9px 0;
  border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 0.85rem;
}
.pd-feed-row:last-child { border-bottom: none; }
.pd-feed-when { color: var(--hd-muted); flex: 0 0 118px; font-size: 0.78rem; }
.pd-feed-store { color: var(--hd-text); flex: 0 0 96px; }
.pd-feed-what { color: var(--hd-text); }
.pd-feed-what .sev { width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 8px; vertical-align: middle; }
.pd-feed-what .sev.high { background: var(--hd-red); }
.pd-feed-what .sev.medium { background: var(--hd-orange); }
.pd-feed-what .sev.low { background: #3fb950; }

.pd-empty { color: var(--hd-muted); font-size: 0.9rem; padding: 8px 0; }

@media (max-width: 640px) {
  .pd-identity { flex-direction: column; }
  .pd-feed-row { flex-wrap: wrap; }
}

@media (prefers-reduced-motion: reduce) {
  a.deal-card { transition: none; }
}
</style>
"""


def apply_theme() -> None:
    """Inject the shared fonts and stylesheet. Call once per page."""
    ui.add_head_html(_THEME)


def render_header(title: str = "HD Clearance Monitor", current_path: str = "/") -> None:
    """Render the fixed header: wordmark, nav, nothing else."""
    apply_theme()
    with ui.header().classes("hd-header items-center justify-between px-6 py-3"):
        with ui.element("div").classes("hd-wordmark"):
            ui.html('CLEARANCE&nbsp;<span class="accent">SCANNER</span>')
        with ui.element("nav").classes("hd-nav flex gap-6"):
            for label, href in _NAV_LINKS:
                cls = "active" if href == current_path else ""
                ui.html(f'<a href="{href}" class="{cls}">{label}</a>')
