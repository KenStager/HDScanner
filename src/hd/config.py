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
    # Admission ceiling for starting a new walk. A walk whose estimated cost
    # would carry the run past this many requests is deferred to the next run
    # instead of being started and then cut mid-page by the quota stop. The
    # distinction matters downstream: a walk cut in flight is recorded
    # "truncated" and can never ground an absence claim, while a walk never
    # attempted writes no coverage row at all — "not attempted is not evidence
    # either". Deferring therefore trades raw rows for trustworthy coverage.
    #
    # 0 falls back to browse_request_budget, i.e. the check only guards against
    # our own hard budget. To buy anything against the API's quota stop this
    # must be set BELOW the depth at which that stop actually lands, which is
    # an installation-specific measurement (it moves with the header profile
    # and the request rate) — hence no shipped default. Derive it from
    # scan_runs: the request_used of runs whose status is "aborted".
    browse_walk_admission_ceiling: int = 0
    # A node walked to "complete" within this many hours is skipped when its
    # category is re-resolved, so a category too big for one run RESUMES
    # instead of restarting. Without it the cursor cannot advance past such a
    # category (it only advances on a finished one), so every subsequent run
    # re-walks the same prefix forever — measured on this install as one
    # category walked 157 times while its siblings were walked once each.
    # Should be a little under the time it takes the rotation to come round,
    # so a category is refreshed once per cycle rather than re-read per run.
    # 0 disables resume entirely and restores the restart behaviour.
    browse_walk_refresh_hours: int = 20
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
    # A short page normally means end-of-results, but Home Depot also serves a
    # short page mid-set (measured: a 299-item node returning 23 of 24 on page
    # 2). When coverage has not yet reached the node's own total, the walk keeps
    # paging and stops only after this many consecutive pages turn up no new
    # itemId — so an OVERSTATED total costs a few pages instead of the walk
    # running to the API ceiling.
    short_page_confirm_pages: int = 2
    # An item counts as "watched" on the dashboard only if the record has
    # actually observed it inside this window. Product rows are only ever
    # activated, never retired, so a raw active-product count is a high-water
    # mark that can never fall — it would keep counting SKUs the retailer has
    # delisted. Deliberately generous: a node is normally re-walked about once
    # a day, so this flags items that have genuinely gone dark, not ordinary
    # rotation lag.
    dashboard_watched_days: int = 7
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
    # Items in the day's set we have never seen carry no brand on the page, so
    # the catalog cannot answer for them. Identifying one costs an API request.
    #
    # Held at 0 after the 2026-09-03 measurement pass. Probing every unknown was
    # tried and reverted the same night for three reasons: the sweep discards a
    # probe that is not our brand (`if not products: continue` precedes the
    # upsert), so the ~110 requests a night would never decay; the obvious fix,
    # recording what was probed, is not a logging change, because
    # _upsert_products marks a row is_active and the snapshot pipeline prices
    # every active product with no brand filter — non-tool deal items would
    # silently join the daily rotation; and the size of the blind spot has never
    # been measured, so there was nothing to weigh the cost against.
    #
    # The "partition" evidence line now records that size for zero requests. Set
    # this once those counts say what probing would actually buy. When raising
    # it, note that the probe targets only ids the catalog has NEVER seen — an
    # id it already answered "not ours" for is not re-requested.
    daily_deals_probe_unknown: int = 0
    # `hd daily-deals --wait-for-refresh` re-reads the page every
    # daily_deals_poll_seconds until the embedded set's end date changes, for
    # at most daily_deals_poll_max reads, then sweeps the new set. Six reads two
    # minutes apart span 3:00 to 3:10 Eastern. Any read that fails or does not
    # parse ends the poll; there is no retry.
    daily_deals_poll_seconds: int = 120
    daily_deals_poll_max: int = 6
    # Installs must not all arrive on the refresh at the same instant (the
    # argument scan_minute makes): the first read waits a per-install 0..N s,
    # and each interval is stretched by a random 0..N s. Only ever delays.
    daily_deals_poll_jitter_seconds: int = 15
    # Reads two minutes apart always land on the same minutes (3:00, 3:02, ...),
    # so the flip is only ever bracketed to the interval that contains it. On
    # alternating nights the first read is held back by this many seconds, which
    # shifts the whole series (3:01, 3:03, ...) and samples the minutes the other
    # phase never sees. Across nights that halves the grid the flip time is known
    # on, for the same six reads — no extra requests. 0 disables the alternation
    # and every night runs the even series. Which night is which is derived from
    # the date, not stored, so a missed night does not flip the sequence and the
    # phase of any past run can be recomputed from its timestamp.
    daily_deals_poll_phase_seconds: int = 60
    # Every read of the page appends one JSON line here: end date, item count,
    # a digest of the item list. The routine sweep reads the page on the runs
    # DAILY_DEALS_HOURS_ET selects (empty = every run), so with it empty this
    # records whether the list changes between reads for one page request per
    # run and no API requests. Priced items are appended before they are
    # written to the database. Gitignored; rolls one generation at 8 MB.
    daily_deals_evidence_path: str = "diagnostics/daily_deals_polls.jsonl"


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
