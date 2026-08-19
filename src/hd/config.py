"""Application configuration via pydantic-settings."""

from __future__ import annotations

from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_csv(v: Any) -> list[str]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        return [item.strip() for item in v.split(",") if item.strip()]
    return []


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = "sqlite+aiosqlite:///./dev.db"

    # API
    api_endpoint: str = "https://apionline.homedepot.com/federation-gateway/graphql"

    # Crawl settings — stored as raw strings, parsed via properties
    # No defaults: these are per-install and undiscoverable without `hd setup`,
    # and a shipped default would silently scan a stranger's stores. Commands
    # that need them fail with a pointer to setup rather than scanning nothing.
    stores: str = ""
    brands: str = ""
    product_line_filters: str = ""
    tools_nav_param: str = "N-5yc1vZc1xy"
    extra_nav_params: str = ""  # Additional category navParams (CSV), no product_line_filter applied
    clearance_token: str = "1z11adf"
    max_concurrency: int = 3
    rate_limit_rps: float = 0.5
    jitter_min_ms: int = 500
    jitter_max_ms: int = 2500
    max_pages: int = 32
    request_budget: int = 100  # Max API requests per HDClient instance (0 = unlimited)
    page_size: int = 24  # 24 is what the browser sends; larger values draw a 403

    # Coverage rotation. A single keyword can need 90+ pages against a ~100
    # request budget, so a run that always starts at page 0 never reaches the
    # tail no matter how often it runs. Each run instead walks a slice starting
    # where the previous run stopped.
    rotation_enabled: bool = True
    rotation_cursor_path: str = ".hd_rotation_cursor"
    rotation_slice_pages: int = 8

    # Pages per keyword for the supplementary storefilter=ALL pass, which picks
    # up online-only items that the IN_STORE filter excludes. Kept small so the
    # pass fits inside the budget instead of being starved by it.
    online_pass_max_pages: int = 3

    # Scan strategy
    scan_keywords: str = ""           # CSV of keyword groups for split scanning (e.g. "Milwaukee M18,Milwaukee M12")
    snapshot_storefilter: str = "ALL" # StoreFilter enum: "ALL", "IN_STORE", or "ONLINE"

    # Facet-driven brand browse (replaces keyword scans when enabled).
    # Keyword search excludes items outside the Tools category and drops some
    # brand items entirely; browse mode walks the brand's own category facets.
    browse_enabled: bool = True
    root_nav_param: str = "N-5yc1v"           # catalog root; brand/category tokens append with Z
    brand_tokens: str = ""                    # CSV of Brand:facet-token, written by `hd setup`
    api_max_start_index: int = 720            # API rejects startIndex > 720 ("Invalid start index range")
    browse_network_categories_per_run: int = 3  # ALL-tier categories walked per store per run
    browse_cursor_path: str = ".hd_browse_cursor"
    browse_request_budget: int = 280          # replaces request_budget for browse runs
    browse_max_split_depth: int = 3           # facet-split recursion guard

    # A deal is only current if its item was seen by a recent scan. Items that
    # drop out of the catalog stop being deals — without this, their last
    # snapshot lingers on the boards forever at a months-old price.
    deal_freshness_hours: int = 48

    # Daily Deals sweep. The daily-deals page refreshes at 3:00 ET; its HTML
    # embeds the day's exact itemId list (specialBuyMetadata dealType=DAY).
    # Each run checks the page and prices the listed items when the set is new,
    # so the 3:10 ET scheduled run captures the fresh set minutes after launch.
    daily_deals_enabled: bool = True
    daily_deals_url: str = "https://www.homedepot.com/daily-deals"
    daily_deals_cursor_path: str = ".hd_dailydeals_cursor"
    daily_deals_max_items: int = 250

    # True-savings verdicts need real history: an item first seen minutes ago
    # has a "30-day high" equal to today's price, which would wrongly label a
    # fresh deal as "flat price". Below this age, say "no price history".
    price_history_min_days: int = 3

    # The honesty chip compares today's price to the highest price we observed
    # in this window, and labels itself with the span it actually saw rather
    # than a fixed "30d" the data may not support. Bounded by
    # snapshot_retention_days — older snapshots are pruned, so a wider window
    # would claim history that no longer exists. At or above this span the
    # label caps at "3mo+". Kept separate from baseline_window_days, which
    # drives alerting in the diff stage. Raising it past snapshot_retention_days
    # is meaningless — those snapshots are gone. Raising it across a period when
    # the scanner was not running is worse than meaningless: today's price gets
    # scored against whatever the catalog looked like before the gap.
    deal_history_window_days: int = 90

    # Inter-keyword pacing
    keyword_pause_min_seconds: float = 3.0
    keyword_pause_max_seconds: float = 8.0

    # Pipeline
    stage_delay_seconds: int = 15

    # Safety
    circuit_breaker_failure_threshold: int = 10
    circuit_breaker_window_seconds: int = 60
    drift_failure_threshold_pct: int = 50

    # Diff
    diff_gap_threshold_hours: int = 48
    diff_stale_gap_hours: int = 168  # 7 days
    baseline_window_days: int = 30
    pricing_error_threshold_pct: int = 75
    cold_start_clearance_pct: int = 40

    # Maintenance
    snapshot_retention_days: int = 90

    # Storage
    store_raw_json: bool = True
    raw_json_dir: str = "./raw_responses"

    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_title: str = "HD Clearance Monitor"
    canvas_title: str = "Deal Rundown"
    dashboard_refresh_seconds: int = 300
    dashboard_dark_mode: bool = True

    # Optional notifiers (v1: unused)
    discord_webhook_url: str = ""
    email_smtp_host: str = ""

    # Slack notifications
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    notify_cursor_path: str = ".hd_notify_cursor"
    canvas_id_path: str = ".hd_canvas_id"
    # The deal rundown canvas is optional and unavailable on free Slack
    # workspaces, which cannot create standalone canvases. Alerts are
    # unaffected when this is off.
    canvas_enabled: bool = True

    @property
    def store_list(self) -> list[str]:
        return _parse_csv(self.stores)

    @property
    def brand_list(self) -> list[str]:
        return _parse_csv(self.brands)

    @property
    def product_line_filter_list(self) -> list[str]:
        return _parse_csv(self.product_line_filters)

    @property
    def extra_nav_param_list(self) -> list[str]:
        return _parse_csv(self.extra_nav_params)

    @property
    def scan_keyword_list(self) -> list[str]:
        return _parse_csv(self.scan_keywords)

    @property
    def brand_token_list(self) -> list[tuple[str, str]]:
        """Parse brand_tokens CSV into (brand, facet_token) pairs, skipping malformed entries."""
        pairs = []
        for entry in _parse_csv(self.brand_tokens):
            brand, sep, token = entry.partition(":")
            if sep and brand.strip() and token.strip():
                pairs.append((brand.strip(), token.strip()))
        return pairs
