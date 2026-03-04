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
    clearance_token: str = "1z11adf"
    max_concurrency: int = 3
    rate_limit_rps: float = 1.0
    jitter_min_ms: int = 200
    jitter_max_ms: int = 800
    max_pages: int = 10
    page_size: int = 24

    # Pipeline
    stage_delay_seconds: int = 5

    # Safety
    circuit_breaker_failure_threshold: int = 10
    circuit_breaker_window_seconds: int = 60
    drift_failure_threshold_pct: int = 50

    # Diff
    diff_gap_threshold_hours: int = 48
    diff_stale_gap_hours: int = 168  # 7 days

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
    slack_channel_id: str = ""
    notify_cursor_path: str = ".hd_notify_cursor"

    @property
    def store_list(self) -> list[str]:
        return _parse_csv(self.stores)

    @property
    def brand_list(self) -> list[str]:
        return _parse_csv(self.brands)

    @property
    def product_line_filter_list(self) -> list[str]:
        return _parse_csv(self.product_line_filters)
