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
    stores: str = "2619,8425"
    brands: str = "Milwaukee"
    product_line_filters: str = "M12,M18"
    tools_nav_param: str = "N-5yc1vZc1xy"
    extra_nav_params: str = ""  # Additional category navParams (CSV), no product_line_filter applied
    clearance_token: str = "1z11adf"
    max_concurrency: int = 3
    rate_limit_rps: float = 0.5
    jitter_min_ms: int = 500
    jitter_max_ms: int = 2500
    max_pages: int = 32
    request_budget: int = 100  # Max API requests per HDClient instance (0 = unlimited)
    page_size: int = 24

    # Scan strategy
    scan_keywords: str = ""           # CSV of keyword groups for split scanning (e.g. "Milwaukee M18,Milwaukee M12")
    snapshot_storefilter: str = "ALL" # StoreFilter enum: "ALL", "IN_STORE", or "ONLINE"

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
    dashboard_refresh_seconds: int = 300
    dashboard_dark_mode: bool = True

    # Optional notifiers (v1: unused)
    discord_webhook_url: str = ""
    email_smtp_host: str = ""

    # OpenClaw / Slack notifications
    openclaw_webhook_url: str = ""
    openclaw_token: str = ""
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    notify_cursor_path: str = ".hd_notify_cursor"
    canvas_id_path: str = ".hd_canvas_id"

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
