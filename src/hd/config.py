"""Application configuration via pydantic-settings."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator
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
    shelf_category_walks: str = ""  # CSV of Label:token — store-wide category nodes walked every shelf pass, every brand captured
    clearance_token: str = "1z11adf"
    max_concurrency: int = 3
    # Tokens available at the start of a run. At the previous value (which
    # borrowed max_concurrency) every run opened with a 3-request burst before
    # any pacing applied — the least restrained moment of the whole run.
    rate_limit_burst: int = 1
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
    # Per-hour tier assignment (US Eastern, CSV). Empty = every run does every
    # tier (the original behaviour). When set, the IN_STORE shelf tier runs only
    # at these hours and the ALL/online tier runs at all other scan hours, so the
    # store can be spread across a couple of runs and the rest spent online.
    browse_shelf_hours_et: str = ""
    browse_cursor_path: str = ".hd_browse_cursor"
    browse_request_budget: int = 280          # replaces request_budget for browse runs
    browse_max_split_depth: int = 3           # facet-split recursion guard
    # Both-ends paging: walk a mid-size node from both price ends (orderBy PRICE
    # ASC + DESC) instead of facet-splitting it, lifting reach from one cap to
    # ~two and collapsing a split into one walk. OFF by default — a staged,
    # observed rollout, guarded at runtime by a union-coverage assertion that
    # marks any seam gap as truncated (never a silent under-cover). A node is
    # eligible when its total is in (reachable_cap, both_ends_cap], where
    # both_ends_cap = 2*reachable_cap - both_ends_min_overlap_pages*page_size.
    # min_overlap 8 keeps ~8 pages of ASC/DESC overlap (cap ~1296) — the safe
    # default; lower to ~5 (cap ~1368) to also fold in the ~1.36k shelf once the
    # assertion has confirmed coverage holds.
    both_ends_paging: bool = False
    both_ends_min_overlap_pages: int = 8
    # Once the second (DESC) pass has seen every item (coverage == live total),
    # walk this many more pages as confirmation, then stop — so a both-ends walk
    # costs ~size, not a flat 2×ceiling, and beats the split it replaces across
    # the whole band. Safe because coverage==total already proves completeness.
    both_ends_confirm_pages: int = 2
    # Fraction of shelf categories walked per run. The shelf tier costs a fixed
    # ~154 page requests every run, six runs a day, to re-read categories that
    # mostly have not changed. At 0.5 each category is seen every other run for
    # half the traffic; 1.0 restores walking all of them every run.
    browse_shelf_fraction: float = 0.5
    # Hours (US Eastern) that walk the whole shelf instead of a slice. Measured
    # over ~2,300 observed changes: regular repricing clusters in the 20:00-04:00
    # window and is finished by 08:00, while new clearance tags appear in the
    # 08:00-12:00 window. A full walk at the close of each window sees the whole
    # store while the change is fresh. Empty disables full walks entirely.
    browse_full_shelf_hours_et: str = "4,12"

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
    # Hours (US Eastern, CSV) the daily-deals sweep runs. Empty = every run (the
    # original behaviour); the set is skipped once seen, so extra runs are cheap.
    daily_deals_hours_et: str = ""
    daily_deals_cursor_path: str = ".hd_dailydeals_cursor"
    daily_deals_max_items: int = 250
    # Items in the day's set that we have never seen are skipped by default:
    # identifying one costs an API request, and across every completed sweep on
    # record none of the ~110 daily deals were a tracked brand. Raise this to
    # spend that many requests probing unknown ids anyway.
    daily_deals_probe_unknown: int = 0


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

    # How long a witnessed low keeps its warning teeth. A "deal" priced above
    # a low we recorded recently is warned — the reader could plausibly have
    # had the better price, and might get it again. Past this age the low
    # stops overruling a real measured drop (deal_tier lets verified evidence
    # outrank it) and rides the card as a dated context chip instead. The
    # durable low never expires, so without this bound every recurring promo
    # eventually sits above some ancient dip and the warning channel numbs —
    # a warning anchored to a months-old price is a cried wolf. Bounded
    # below: zero or negative would silently disable every dated-low
    # warning, and this is the sole dial on a load-bearing honesty behavior.
    warn_low_recency_days: int = Field(default=45, ge=1)

    # Inter-keyword pacing
    keyword_pause_min_seconds: float = 3.0
    keyword_pause_max_seconds: float = 8.0

    # Pipeline
    stage_delay_seconds: int = 15

    # Client identity. The scanner names itself rather than borrowing a
    # browser's User-Agent: that is what lets the operator on the other end
    # allow-list it, throttle it deliberately, or ask it to stop. Set
    # contact_email so a human there can reach a human here.
    user_agent: str = "HDClearanceMonitor/0.1"
    contact_email: str = ""
    # Optional named header profile. Blank keeps the honest identity above; a
    # local override module may recognise other values. Public installs leave it
    # blank and send the tool identity.
    header_profile: str = ""

    # Request limits. There is no connection pooling: the API refuses Python
    # HTTP clients outright, so every request is its own curl process.
    read_timeout_seconds: float = 30.0
    max_response_bytes: int = 10 * 1024 * 1024

    # Retry policy. A Retry-After longer than the ceiling ends the run instead
    # of being waited out — at that point the API is asking for a later visit,
    # not a slower one.
    max_attempts: int = 5

    # How long to stay away after an outright refusal — a 403, or an HTML
    # block page served where JSON was expected. Both stop the run.
    forbidden_cooldown_seconds: float = 3600.0

    # A 206 quota signal applies to the caller, not the process. Without a
    # cooldown that survives exit, the next scheduled run reopens a client and
    # walks straight back into the wall — which is what happened at 20:00 on
    # 2026-08-19, throttled on its second request.
    throttle_cooldown_path: str = ".hd_throttle_cooldown"
    throttle_cooldown_seconds: float = 3600.0
    max_retry_after_seconds: float = 300.0

    # Baseline instrumentation. Each run appends one summary line (success
    # rate, latency percentiles, status/outcome counts) so the API's behaviour
    # can be characterised across runs — a single run is far too small a
    # sample to draw anything from.
    metrics_path: str = "diagnostics/http_metrics.jsonl"

    # Scan liveness. Notifications fire on transitions — stopped, resumed —
    # not on state, so a long outage is two messages instead of one suppressed
    # database row nobody reads.
    health_state_path: str = ".hd_health_state"
    health_notify: bool = True

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
    # A snapshot row is two things at once: the record (price, promo,
    # clearance, inventory — a few hundred bytes) and the receipt (the raw
    # API response it was parsed from, ~1.9 KB and 71% of the database).
    # Deleting whole rows at retention age throws away the record to get rid
    # of the receipt. Slimming splits the fates: rows older than this keep
    # every parsed field but drop raw_json, so point-by-point price history
    # survives at ~15% of the weight and snapshot_retention_days can be set
    # years out instead of months. 0 disables slimming (rows stay whole
    # until deleted). Meaningful only below snapshot_retention_days — a
    # slim age past the delete age never fires, and `hd prune` says so.
    snapshot_slim_days: int = 0
    # SQLite keeps the pages a delete frees and reuses them later, so pruning
    # alone never shrinks the file: after deleting 491,902 rows the database
    # still measured 1.43 GB with 1.26 GB reclaimable. VACUUM returns the space
    # but rewrites the whole file, so it runs only once the waste is worth the
    # rewrite. 0 disables it.
    vacuum_threshold_pct: int = 25

    # Backups. The database is the record; the machine it lives on is a
    # single point of failure until a snapshot exists somewhere else. Each
    # directory gets a verified VACUUM INTO snapshot per `hd backup` run,
    # rotated to backup_keep files. CSV so one run can feed a second drive
    # and a local directory; empty disables the command with a hint.
    backup_dirs: str = ""
    backup_keep: int = 14

    # Storage
    store_raw_json: bool = True
    raw_json_dir: str = "./raw_responses"
    # Raw responses are the receipts behind each parse. Nothing reads them
    # programmatically, but they are what makes after-the-fact forensics
    # possible — the per-category cost and clearance-yield analysis of
    # 2026-08-20 came entirely from this directory.
    #
    # They accumulated at ~59 MB/day and nothing ever deleted them, reaching
    # 3,559 files and 353 MB in six days. A week keeps enough to investigate a
    # bad parse while bounding the directory; raise it for deeper forensics,
    # set 0 to keep everything.
    raw_retention_days: int = 7

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

    # Stores you deliberately stopped scanning but whose price history you
    # kept. Without this, `hd doctor` warns about their leftover data forever
    # and the dashboard advisory never clears — so a permanent, chosen state
    # would look identical to a problem.
    retired_stores: str = ""

    # Scan cadence, as Eastern hours. Empty uses the shipped three-a-day
    # schedule. Raising it costs allowance that is shared across every install
    # of this tool, not just yours — see setup_schedule.SCAN_HOURS_ET.
    scan_hours_et: str = ""
    # Minute past the hour. None derives one from this install's path so that
    # many installs do not all fire on the same minute.
    scan_minute: int | None = None

    @property
    def store_list(self) -> list[str]:
        return _parse_csv(self.stores)

    @property
    def retired_store_list(self) -> list[str]:
        return _parse_csv(self.retired_stores)

    @property
    def scan_hours_et_list(self) -> list[int]:
        hours = []
        for raw in _parse_csv(self.scan_hours_et):
            try:
                hours.append(int(raw) % 24)
            except ValueError:
                continue
        return sorted(set(hours))

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

    @property
    def shelf_category_walk_list(self) -> list[tuple[str, str]]:
        """Parse shelf_category_walks CSV into (label, facet_token) pairs, skipping malformed entries."""
        pairs = []
        for entry in _parse_csv(self.shelf_category_walks):
            label, sep, token = entry.partition(":")
            if sep and label.strip() and token.strip():
                pairs.append((label.strip(), token.strip()))
        return pairs
