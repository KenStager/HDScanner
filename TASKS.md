# TASKS — Home Depot Clearance Monitor (Claude Code Build Plan)

**Build order is strict. Do not start a milestone until the prior milestone's acceptance gate passes.**  
Each task is a discrete, testable unit of work. Check off tasks as they are completed.

---

## M0 — Repo Scaffolding & Config

**Goal:** Working skeleton. Database initializes. Config loads. Logging works.

### Tasks

- [ ] **M0.1** — Create repo structure exactly as defined in SPEC.md §3. Create all directories and empty `__init__.py` files.
- [ ] **M0.2** — Create `pyproject.toml` with dependencies:
  - `httpx[brotli]>=0.27`
  - `sqlalchemy[asyncio]>=2.0`
  - `alembic>=1.13`
  - `asyncpg` (Postgres driver)
  - `aiosqlite` (SQLite dev driver)
  - `pydantic-settings>=2.0`
  - `typer>=0.12`
  - `structlog>=24.0`
  - `tenacity>=8.0`
  - `pytest`, `pytest-asyncio`, `pytest-mock`
- [ ] **M0.3** — Implement `src/hd/config.py` — `pydantic-settings` Config class. Must load all env vars from SPEC.md §4. Parse `STORES` as `list[str]` and `BRANDS` as `list[str]`.
- [ ] **M0.4** — Implement `src/hd/logging.py` — structlog configured for JSON output. Provide `get_logger(name)` helper.
- [ ] **M0.5** — Implement `src/hd/db/models.py` — all four ORM models: `Product`, `Store`, `StoreSnapshot`, `Alert` with all fields from SPEC.md §5.1. `AlertType` and `Severity` enums defined here.
- [ ] **M0.6** — Implement `src/hd/db/base.py` — async SQLAlchemy engine factory. Must support both Postgres and SQLite URLs. Provide `get_session()` async context manager.
- [ ] **M0.7** — Create initial Alembic migration (`alembic init` + `env.py` wired to async engine + first migration generating all 4 tables).
- [ ] **M0.8** — Implement `hd init-db` CLI command in `cli.py` — runs Alembic upgrade to head.
- [ ] **M0.9** — Implement `hd add-store` CLI command. Pre-seed stores 2619 and 8425 as part of `init-db` automatically.
- [ ] **M0.10** — Create `.env.example` matching SPEC.md §4 exactly.

### Acceptance Gate M0

```bash
# These must all succeed before M1 begins
cp .env.example .env         # fill in DATABASE_URL
hd init-db                   # exits 0, tables exist
hd add-store 9999 --name "Test Store"   # exits 0, row in stores table
python -c "from hd.config import Settings; s = Settings(); print(s.stores)"  # prints ['2619', '8425']
```

---

## M1 — HTTP Client & Discovery Pipeline

**Goal:** Can call Home Depot GraphQL API, parse product list, store results.

### Tasks

- [ ] **M1.1** — Create `queries/searchModel.graphql` — full GraphQL query string from the API guide (Research Guide document). Must request all fields listed in SPEC.md §6.3.
- [ ] **M1.2** — Implement `src/hd/http/rate_limit.py` — async token bucket rate limiter. Constructor takes `rps: float` and `burst: int`. Provides `async def acquire()` method that blocks until token available, then applies random jitter per config.
- [ ] **M1.3** — Implement `src/hd/http/client.py` — thin wrapper around `httpx.AsyncClient`. Sets all required headers from SPEC.md §6.2 by default. Injects rate limiter. Wraps request with tenacity retry decorator per SPEC.md §8.
- [ ] **M1.4** — Implement `src/hd/hd_api/models.py` — `NormalizedProduct` and `NormalizedSnapshot` dataclasses per SPEC.md §5.2.
- [ ] **M1.5** — Implement `src/hd/hd_api/graphql.py` — `async def search(keyword, nav_param, store_id, start_index, page_size)` function. Loads query from `queries/searchModel.graphql`. Builds variables dict per SPEC.md §6.4. Returns raw JSON dict.
- [ ] **M1.6** — Implement `src/hd/hd_api/parsers.py`:
  - `parse_products(raw_response: dict) -> list[NormalizedProduct]` — maps response paths per SPEC.md §6.5. Must handle all fields as nullable (no KeyError on missing fields).
  - `parse_snapshots(raw_response: dict, store_id: str) -> list[NormalizedSnapshot]` — same, with inventory extraction from fulfillment object. Must find the correct location by `locationId == store_id`.
- [ ] **M1.7** — Implement `src/hd/pipeline/discovery.py` — `async def run_discovery(brands, nav_param, pages)`. Paginates through searchModel results per brand. Filters by `brandName in config.BRANDS`. Upserts into `products` table (insert on conflict update `last_seen_ts`, `is_active=True`).
- [ ] **M1.8** — Implement `hd discover` CLI command. Accepts `--brand`, `--pages`, `--clearance-only` flags.
- [ ] **M1.9** — Write `tests/test_parsers.py`:
  - Save a real API response to `tests/fixtures/sample_searchModel_response.json` (make one real call or use a mock)
  - Test that `parse_products` returns list with expected fields
  - Test missing `pricing.promotion` doesn't crash — returns None fields
  - Test missing `fulfillment` doesn't crash

### Acceptance Gate M1

```bash
hd discover --brand Milwaukee --brand DEWALT --pages 3
# Check DB:
# products table has >= 50 rows
# all rows have item_id, brand, title populated
# last_seen_ts is recent
```

---

## M2 — Snapshot Fetching

**Goal:** For each active product × each store, fetch and persist a snapshot.

### Tasks

- [ ] **M2.1** — Implement `src/hd/pipeline/snapshot.py` — `async def run_snapshots(store_ids, limit)`. Loads active products from DB. For each (store_id, item_id), calls graphql.search with appropriate parameters to get pricing + inventory for that product at that store. Parses → `NormalizedSnapshot`. Inserts into `store_snapshots` (always INSERT, never UPDATE — append only). Respects `MAX_CONCURRENCY` semaphore.
  - **Strategy note:** Query by `keyword="{model_number}"` for per-product lookups, falling back to `keyword="{title[:30]}"`. This avoids re-fetching full category pages per snapshot cycle.
- [ ] **M2.2** — Implement `hd snapshot` CLI command. Accepts `--stores` (comma-separated), `--limit`.
- [ ] **M2.3** — Implement optional raw JSON storage: if `STORE_RAW_JSON=true`, write response to `RAW_JSON_DIR/{item_id}_{store_id}_{timestamp}.json`.
- [ ] **M2.4** — Add `raw_json` JSONB field to `StoreSnapshot` model and Alembic migration. Populate it in snapshot pipeline.

### Acceptance Gate M2

```bash
hd snapshot --stores 2619,8425 --limit 20
# Check DB:
# store_snapshots has >= 20 rows (10 per store if 10 products)
# price_value is non-null for most rows
# inventory_qty is populated for some rows
# ts is recent
# No duplicate rows (same store_id + item_id + ts should be unique within a run)
```

---

## M3 — Diff Engine & Alerts

**Goal:** Detect events and write alerts to the database.

### Tasks

- [ ] **M3.1** — Implement `src/hd/pipeline/diff.py` — `async def run_diff()`. For each (store_id, item_id) in `store_snapshots`, fetches the two most recent rows ordered by `ts`. Applies diff logic from SPEC.md §7.3. Returns list of `Alert` objects (not yet written to DB).
- [ ] **M3.2** — Implement `src/hd/pipeline/alerts.py` — `async def write_alerts(alerts: list[Alert])`. Bulk inserts alert rows. Builds `payload` JSONB with `before`/`after` snapshot fields + product title + store name + product URL.
- [ ] **M3.3** — Implement `hd alerts` CLI command. Prints recent alerts in a readable table: timestamp, store, product title, alert type, severity, percentage off.
- [ ] **M3.4** — Write `tests/test_diff.py`:
  - Test PRICE_DROP: `curr.price_value < prev.price_value` → alert generated
  - Test CLEARANCE: `curr.savings_center = "CLEARANCE"`, `prev.savings_center = None` → alert
  - Test SPECIAL_BUY: `curr.special_buy = True`, `prev.special_buy = False` → alert
  - Test BACK_IN_STOCK: flip → alert
  - Test OOS: flip → alert
  - Test no alert when nothing changed
  - Test first snapshot (no prev) → no alert

### Acceptance Gate M3

```bash
# Insert synthetic "before" snapshot manually, run snapshot (gets new "after"), then:
hd run-once   # (partial: just diff + alerts portion)

# In DB:
# alerts table has at least one row if any price/clearance/stock change occurred
# payload JSONB contains before/after values
# severity populated correctly
```

---

## M4 — Full Pipeline, Resilience & Health

**Goal:** End-to-end `run-once`, circuit breaker, and schema drift detection.

### Tasks

- [ ] **M4.1** — Implement `src/hd/pipeline/health.py` — `check_drift(response_pages: list[dict]) -> HealthStatus`. Checks each critical path from SPEC.md §7.5 across all products in the response. If >50% missing → returns `DEGRADED`. Writes a `HEALTH_DEGRADED` alert to DB.
- [ ] **M4.2** — Implement circuit breaker in `http/client.py`. Counts failures per rolling window. If `CIRCUIT_BREAKER_FAILURE_THRESHOLD` exceeded, raises `CircuitOpenError` and stops crawling.
- [ ] **M4.3** — Integrate `health.check_drift()` into discovery and snapshot pipelines — call after each page fetch. Stop and emit alert if DEGRADED.
- [ ] **M4.4** — Implement `hd run-once` CLI command — runs full pipeline in order: discover → snapshot → diff → alerts. Logs summary at end: N products discovered, N snapshots taken, N alerts generated.
- [ ] **M4.5** — Implement `hd health` CLI command. Prints last run stats and `source_health` status.
- [ ] **M4.6** — Write `tests/test_health.py`:
  - Mock response missing `pricing.value` in >50% of products → assert DEGRADED returned
  - Mock response with all fields present → assert HEALTHY returned

### Acceptance Gate M4

```bash
hd run-once
# Exits 0
# Logs show: "Discovery complete: N products", "Snapshots complete: N rows", "Diff complete: N alerts"

# Simulate drift: patch parsers.py to make pricing.value always missing
# hd run-once should exit non-zero and insert HEALTH_DEGRADED alert into alerts table
hd health   # prints DEGRADED status
```

---

## M5 — Cleanup, README & Smoke Test

**Goal:** Repo is clean, documented, and runs reliably end to end.

### Tasks

- [ ] **M5.1** — Write `README.md` covering: setup, `.env` configuration, store IDs, CLI commands, how to add new stores, how to read alerts.
- [ ] **M5.2** — Ensure all `pytest` tests pass: `pytest tests/ -v`
- [ ] **M5.3** — Add `hd discover --clearance-only` support: appends clearance token to navParam, runs discovery, marks discovered products with a `clearance_flag` boolean in `products` table. (Add Alembic migration for new column.)
- [ ] **M5.4** — Run full smoke test:
  ```bash
  hd init-db
  hd run-once
  hd alerts --limit 20
  ```
  Verify: real data in DB, no unhandled exceptions, alerts generated or empty table with no errors.
- [ ] **M5.5** — Verify raw JSON files written to `RAW_JSON_DIR` when `STORE_RAW_JSON=true`. Confirm they are valid JSON.

### Acceptance Gate M5

```bash
pytest tests/ -v        # all tests pass
hd run-once             # exits 0 with real API data
hd alerts --limit 10    # prints table (may be empty if no events yet — that is OK)
```

---

## Notes for Claude Code

- **Do not move to the next milestone** until the current acceptance gate passes in a real terminal.
- **Never hardcode** store IDs, brand names, navparam tokens, or API URLs. All must come from `config.py`.
- **Never inline** the GraphQL query string in Python code. Always load from `queries/searchModel.graphql`.
- **Parser functions must never raise** on missing fields. Use `dict.get()` chains or `try/except` for all response field extractions.
- **`store_snapshots` is append-only.** Never UPDATE a snapshot row. Never DELETE snapshot rows.
- If an API call returns 0 products for a page, stop pagination — do not treat as an error.
- Commit after each milestone gate passes (or at minimum after each task group).
