# SPEC — Home Depot Clearance Monitor (Technical Specification)

**Status:** v1.0 — Active  
**Last Updated:** 2026-02-26  

---

## 1. System Overview

The system is a **single-process async Python application** with a CLI entry point. It runs a pipeline of:

```
Discovery → Snapshot → Normalize → Diff → Alert
```

All state is persisted in a Postgres database (SQLite for local dev). Raw API responses are optionally stored as JSONB or flat files for debugging and schema replay.

---

## 2. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Async-first |
| HTTP client | `httpx` (async) | Supports gzip/br, connection pooling |
| Database (prod) | PostgreSQL | Via SQLAlchemy 2.x async + Alembic |
| Database (dev) | SQLite | Same ORM, no server needed |
| CLI | `typer` | Type-annotated commands |
| Config | `pydantic-settings` + `.env` | Validated at startup |
| Logging | `structlog` | JSON-structured logs |
| Retry/backoff | `tenacity` | Decorator-based retry with jitter |
| Testing | `pytest` + `pytest-asyncio` | Fixture-based |

---

## 3. Repository Structure

```
hd-clearance-monitor/
  README.md
  PRD.md
  SPEC.md
  TASKS.md
  CLAUDE.md
  pyproject.toml
  .env.example
  .gitignore
  queries/
    searchModel.graphql          # versioned GraphQL query string
  src/
    hd/
      __init__.py
      config.py                  # pydantic-settings Config class
      logging.py                 # structlog setup
      http/
        client.py                # httpx async client wrapper
        rate_limit.py            # token bucket + jitter implementation
      hd_api/
        graphql.py               # builds and sends GraphQL requests
        parsers.py               # raw JSON → NormalizedSnapshot
        models.py                # dataclasses: NormalizedProduct, NormalizedSnapshot
      db/
        base.py                  # SQLAlchemy engine + session factory
        models.py                # ORM models (Product, Store, StoreSnapshot, Alert)
        migrations/              # Alembic migration files
          env.py
          versions/
      pipeline/
        discovery.py             # F1: product discovery
        snapshot.py              # F2: store snapshot fetching
        diff.py                  # F3: diff engine
        alerts.py                # F3: alert writer
        health.py                # F6: schema drift detector
      cli.py                     # typer app, all commands
  tests/
    conftest.py
    fixtures/
      sample_searchModel_response.json
    test_parsers.py
    test_diff.py
    test_health.py
```

---

## 4. Configuration (`.env.example`)

```
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/hd_monitor
# Use sqlite+aiosqlite:///./dev.db for local dev

# Crawl settings
STORES=2619,8425
BRANDS=Milwaukee,DEWALT
TOOLS_NAV_PARAM=N-5yc1vZc1xy
CLEARANCE_TOKEN=1z11adf
MAX_CONCURRENCY=3
RATE_LIMIT_RPS=1.0
JITTER_MIN_MS=200
JITTER_MAX_MS=800
MAX_PAGES=10
PAGE_SIZE=24

# Safety
CIRCUIT_BREAKER_FAILURE_THRESHOLD=10
CIRCUIT_BREAKER_WINDOW_SECONDS=60
DRIFT_FAILURE_THRESHOLD_PCT=50

# Storage
STORE_RAW_JSON=true
RAW_JSON_DIR=./raw_responses

# Optional notifiers (v1: unused, leave blank)
DISCORD_WEBHOOK_URL=
EMAIL_SMTP_HOST=
```

---

## 5. Data Models

### 5.1 ORM Models (`db/models.py`)

#### `products`
```python
class Product(Base):
    __tablename__ = "products"
    item_id: str (PK)
    brand: str                  # "Milwaukee" | "DEWALT"
    title: str
    canonical_url: str
    model_number: str | None
    first_seen_ts: datetime
    last_seen_ts: datetime
    is_active: bool = True
```

#### `stores`
```python
class Store(Base):
    __tablename__ = "stores"
    store_id: str (PK)
    name: str | None
    state: str | None
    zip: str | None
```

#### `store_snapshots` (append-only)
```python
class StoreSnapshot(Base):
    __tablename__ = "store_snapshots"
    id: int (PK autoincrement)
    ts: datetime
    store_id: str (FK → stores)
    item_id: str (FK → products)
    price_value: Decimal | None
    price_original: Decimal | None
    promotion_type: str | None
    promotion_tag: str | None
    savings_center: str | None       # e.g. "CLEARANCE"
    dollar_off: Decimal | None
    percentage_off: int | None
    special_buy: bool | None
    inventory_qty: int | None
    in_stock: bool | None
    limited_qty: bool | None
    out_of_stock: bool | None
    raw_json: dict | None            # JSONB (Postgres) or JSON (SQLite)
```

#### `alerts`
```python
class Alert(Base):
    __tablename__ = "alerts"
    id: int (PK autoincrement)
    ts: datetime
    store_id: str (FK → stores)
    item_id: str (FK → products)
    alert_type: AlertType            # ENUM below
    severity: Severity               # ENUM below
    payload: dict                    # JSONB with before/after values

class AlertType(str, Enum):
    PRICE_DROP = "PRICE_DROP"
    CLEARANCE = "CLEARANCE"
    SPECIAL_BUY = "SPECIAL_BUY"
    BACK_IN_STOCK = "BACK_IN_STOCK"
    OOS = "OOS"
    HEALTH_DEGRADED = "HEALTH_DEGRADED"

class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
```

---

### 5.2 Internal Dataclasses (`hd_api/models.py`)

```python
@dataclass
class NormalizedProduct:
    item_id: str
    brand: str | None
    title: str | None
    canonical_url: str | None
    model_number: str | None

@dataclass
class NormalizedSnapshot:
    item_id: str
    store_id: str
    ts: datetime
    price_value: float | None
    price_original: float | None
    promotion_type: str | None
    promotion_tag: str | None
    savings_center: str | None
    dollar_off: float | None
    percentage_off: int | None
    special_buy: bool | None
    inventory_qty: int | None
    in_stock: bool | None
    limited_qty: bool | None
    out_of_stock: bool | None
    raw: dict                    # full raw product object from API
```

---

## 6. API Integration

### 6.1 Endpoint

```
POST https://apionline.homedepot.com/federation-gateway/graphql?opname=searchModel
```

### 6.2 Required Headers

```python
HEADERS = {
    "Host": "apionline.homedepot.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.homedepot.com/",
    "Content-Type": "application/json",
    "Origin": "https://www.homedepot.com",
    "x-experience-name": "general-merchandise",
    "x-hd-dc": "origin",
    "x-debug": "false",
}
```

### 6.3 GraphQL Query File (`queries/searchModel.graphql`)

The full query string is stored in `queries/searchModel.graphql` and loaded at runtime. It must include these return fields at minimum:

```
products {
  itemId
  identifiers { brandName modelNumber canonicalUrl productLabel storeSkuNumber itemId }
  pricing(storeId: $storeId) {
    value original
    promotion {
      type description dollarOff percentageOff
      promotionTag savingsCenter savingsCenterPromos
      specialBuySavings specialBuyDollarOff specialBuyPercentageOff
    }
    specialBuy
  }
  fulfillment {
    fulfillmentOptions {
      type services {
        type
        locations {
          locationId storeName type isAnchor
          inventory {
            isOutOfStock isInStock isLimitedQuantity quantity
          }
        }
      }
    }
  }
  availabilityType { discontinued buyable status }
  badges { name label }
}
```

### 6.4 Request Variables

```python
variables = {
    "keyword": keyword,           # "Milwaukee" | "DEWALT" | None
    "navParam": nav_param,        # e.g. "N-5yc1vZc1xyZ1z11adf"
    "storeId": store_id,          # "2619" | "8425"
    "storefilter": "ALL",
    "channel": "DESKTOP",
    "isBrandPricingPolicyCompliant": False,
    "skipInstallServices": True,
    "skipFavoriteCount": True,
    "skipDiscoveryZones": True,
    "skipBuyitagain": True,
    "additionalSearchParams": {
        "deliveryZip": "",
        "multiStoreIds": []
    },
    "filter": {},
    "orderBy": {"field": "BEST_MATCH", "order": "ASC"},
    "pageSize": 24,
    "startIndex": start_index,
}
```

### 6.5 Response Path Mapping

| Field | JSON Path |
|---|---|
| Item ID | `data.searchModel.products[].itemId` |
| Brand | `data.searchModel.products[].identifiers.brandName` |
| Title | `data.searchModel.products[].identifiers.productLabel` |
| Canonical URL | `data.searchModel.products[].identifiers.canonicalUrl` |
| Model Number | `data.searchModel.products[].identifiers.modelNumber` |
| Price (current) | `data.searchModel.products[].pricing.value` |
| Price (original) | `data.searchModel.products[].pricing.original` |
| Promotion type | `data.searchModel.products[].pricing.promotion.type` |
| Promotion tag | `data.searchModel.products[].pricing.promotion.promotionTag` |
| Savings center | `data.searchModel.products[].pricing.promotion.savingsCenter` |
| Dollar off | `data.searchModel.products[].pricing.promotion.dollarOff` |
| Percentage off | `data.searchModel.products[].pricing.promotion.percentageOff` |
| Special buy | `data.searchModel.products[].pricing.specialBuy` |
| Store inventory | `data.searchModel.products[].fulfillment.fulfillmentOptions[type=pickup].services[].locations[locationId={store_id}].inventory` |
| Inventory qty | `...inventory.quantity` |
| In stock | `...inventory.isInStock` |
| Limited qty | `...inventory.isLimitedQuantity` |
| Out of stock | `...inventory.isOutOfStock` |

### 6.6 Pagination

Increment `startIndex` by `pageSize` (24) per page. Stop when the returned products count < `pageSize` or `startIndex >= MAX_PAGES * pageSize`.

---

## 7. Pipeline Logic

### 7.1 Discovery (`pipeline/discovery.py`)

```
For each brand in [Milwaukee, DEWALT]:
    For each page until exhausted or MAX_PAGES:
        POST searchModel(keyword=brand, navParam=TOOLS_NAV_PARAM, storeId=STORES[0], startIndex=page*24)
        Parse response → list[NormalizedProduct]
        Filter: keep only where brandName in {"Milwaukee","DEWALT"}
        Upsert into `products` (update last_seen_ts, is_active=True)
```

Optionally run a second pass with `navParam = TOOLS_NAV_PARAM + "Z" + CLEARANCE_TOKEN` to surface clearance-only SKUs.

### 7.2 Snapshot Fetcher (`pipeline/snapshot.py`)

```
Load all active products from DB
For each (store_id, item_id) pair (batched by store):
    POST searchModel(keyword=None, navParam based on itemId or category, storeId=store_id)
    NOTE: The most reliable approach is to query by keyword=item_id or use product-level detail op
    Parse → NormalizedSnapshot
    INSERT into store_snapshots (never UPDATE — append only)
    If STORE_RAW_JSON=true: write raw JSON to RAW_JSON_DIR/{item_id}_{store_id}_{ts}.json
```

**Note:** For snapshot fetching per-item, prefer querying by `keyword="{model_number}"` or the item ID directly if the API supports it, rather than fetching full category pages repeatedly.

### 7.3 Diff Engine (`pipeline/diff.py`)

```
For each (store_id, item_id):
    prev = most recent snapshot before current run
    curr = snapshot from current run

    if curr is None: skip
    if prev is None: no diff (first time seen)

    if curr.price_value < prev.price_value:
        emit PRICE_DROP alert
        severity = HIGH if pct_drop > 50% else MEDIUM if pct_drop > 25% else LOW

    if curr.savings_center == "CLEARANCE" and prev.savings_center != "CLEARANCE":
        emit CLEARANCE alert
        severity = HIGH if curr.percentage_off >= 50 else MEDIUM

    if curr.special_buy and not prev.special_buy:
        emit SPECIAL_BUY alert, severity = MEDIUM

    if curr.in_stock and not prev.in_stock:
        emit BACK_IN_STOCK alert, severity = LOW

    if not curr.in_stock and prev.in_stock:
        emit OOS alert, severity = LOW
```

### 7.4 Alert Writer (`pipeline/alerts.py`)

All alerts are written to the `alerts` table with a JSONB `payload` containing:

```json
{
  "before": { ...prev snapshot fields },
  "after": { ...curr snapshot fields },
  "product_title": "...",
  "store_name": "...",
  "product_url": "https://www.homedepot.com/p/..."
}
```

### 7.5 Schema Drift Detector (`pipeline/health.py`)

Critical paths monitored on every run:

```python
CRITICAL_PATHS = [
    "pricing.value",
    "pricing.original",
    "pricing.promotion.savingsCenter",
    "pricing.promotion.percentageOff",
    "fulfillment.fulfillmentOptions",
    "identifiers.brandName",
    "identifiers.productLabel",
]
```

If any critical path is missing in >50% of products in a response page, emit a `HEALTH_DEGRADED` alert and set a `source_health` flag to stop further crawling.

---

## 8. Rate Limiting & Retry

### Rate limiter (`http/rate_limit.py`)

Token bucket implementation:
- 1 token added per `1/RATE_LIMIT_RPS` seconds
- Burst capacity: `MAX_CONCURRENCY` tokens
- Each request consumes 1 token; waits if bucket is empty
- After acquiring token, sleep additional random jitter `[JITTER_MIN_MS, JITTER_MAX_MS]` ms

### Retry decorator

Using `tenacity`:
```python
@retry(
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)),
    before_sleep=log_retry,
)
```

Retryable status codes: 429, 500, 502, 503, 504.  
Non-retryable: 400, 403 (treat 403 as potential block — log warning, do not retry aggressively).

---

## 9. CLI Commands (`cli.py`)

```
hd init-db
    Creates all tables via Alembic migration. Safe to re-run.

hd add-store <store_id> [--name NAME] [--state STATE] [--zip ZIP]
    Inserts a store record. Stores 2619 and 8425 pre-seeded.

hd discover [--brand BRAND]... [--pages N] [--clearance-only]
    Runs discovery pipeline. Default brands: Milwaukee, DEWALT.

hd snapshot [--stores STORE_ID,...]  [--limit N]
    Fetches snapshots for active products at specified stores.
    Defaults to configured STORES.

hd run-once
    Runs full pipeline: discover → snapshot → diff → alerts.

hd alerts [--limit N] [--type TYPE] [--since HOURS]
    Prints recent alerts from DB in human-readable table.

hd health
    Prints source_health status and last run stats.
```

---

## 10. Testing Strategy

### Unit Tests (required)

- `test_parsers.py` — parse fixture JSON into NormalizedProduct and NormalizedSnapshot; assert all fields mapped correctly including missing/null cases
- `test_diff.py` — test each alert type trigger with constructed before/after snapshot pairs
- `test_health.py` — test drift detector with mocked responses missing critical paths

### Integration / Smoke Test (manual)

- Run `hd run-once` against real API with 1 store + 5 product limit
- Assert at least 1 snapshot row inserted per product
- Assert alerts table either empty (no changes) or populated with valid rows

### Fixtures

- Save a real `searchModel` response to `tests/fixtures/sample_searchModel_response.json` on first successful run; use it for all unit tests thereafter

---

## 11. Error Handling Matrix

| Scenario | Behavior |
|---|---|
| HTTP 429 | Exponential backoff, up to 5 retries |
| HTTP 403 | Log warning, pause 30s, do not retry aggressively |
| HTTP 5xx | Exponential backoff, up to 5 retries |
| Missing JSON field | Return None for that field; never crash parser |
| DB connection failure | Raise and stop pipeline; log error |
| Drift detected | Emit HEALTH_DEGRADED alert, stop crawl, exit non-zero |
| All products return 0 results | Emit HEALTH_DEGRADED alert, investigate navParam/tokens |

---

## 12. Versioning & Stability Notes

- The GraphQL query is versioned in `queries/searchModel.graphql`. Do not inline it.
- If the endpoint changes, only `hd_api/graphql.py` and the query file need to change.
- Navparam tokens (`N-5yc1vZc1xy`, `1z11adf`) are empirically observed and may drift. Treat them as config values, not hardcoded strings.
- The `brandName` filter (`"Milwaukee"`, `"DEWALT"`) is the most stable brand identifier—prefer it over token-based brand filtering.
