# PRD — Home Depot Clearance Monitor (Milwaukee + DeWalt)

**Status:** v1.0 — Active  
**Last Updated:** 2026-02-26  
**Owner:** Ken  
**Audience:** Claude Code, developers  

---

## 1. Problem Statement

Milwaukee and DeWalt tools go on deep clearance and sale at Home Depot stores frequently, but these events are transient, store-specific, and not surfaced through any public alert system. By the time a buyer notices a deal it may be gone—or available at a nearby store but not the local one. There is no programmatic way for a buyer to track real-time pricing, promotion flags, and inventory counts across specific stores for these two brands.

---

## 2. Goal

Build a backend monitoring system that:

- Continuously tracks Milwaukee and DeWalt tool products at **stores 2619 and 8425**
- Detects clearance events, price drops, special buys, and back-in-stock transitions
- Stores historical snapshots for trend analysis
- Produces actionable alerts when thresholds are crossed

---

## 3. Users

**Primary user:** Ken (solo operator). System is personal-use, self-hosted, CLI-driven.  
**Secondary:** Potential expansion to additional stores or users later (v2+).

---

## 4. Success Criteria

| Metric | Target |
|---|---|
| Products tracked | ≥ 200 Milwaukee + DeWalt SKUs per discovery run |
| Snapshot freshness | Snapshots taken ≥ once per run cycle per (store, product) |
| Clearance detection | `pricing.promotion.savingsCenter == "CLEARANCE"` correctly flagged |
| Alert latency | Alert generated within one run cycle of first detecting an event |
| Schema drift tolerance | System pauses gracefully on breaking API changes (no silent bad data) |
| Data integrity | Zero duplicate snapshots; append-only snapshot table |

---

## 5. Features (v1 Scope)

### F1 — Product Discovery
- Search Home Depot's internal GraphQL API for Milwaukee and DeWalt tools
- Use keyword (`"Milwaukee"`, `"DEWALT"`) combined with Tools category navParam
- Optionally layer in clearance filter navParam token
- Store discovered products in `products` table with brand, title, model number, URL

### F2 — Store Snapshot Fetching
- For each discovered product × each monitored store (2619, 8425), fetch:
  - Current price and original price
  - Promotion type, tag, savings center flag
  - Dollar off and percentage off values
  - In-stock, out-of-stock, limited quantity flags
  - Exact inventory quantity
- Append each fetch result as an immutable row in `store_snapshots`

### F3 — Diff Engine / Alert Generation
- Compare latest snapshot vs previous snapshot for each (store_id, item_id) pair
- Generate alerts for:
  - `PRICE_DROP` — value decreased from prior snapshot
  - `CLEARANCE` — `savings_center` became `"CLEARANCE"`
  - `SPECIAL_BUY` — `special_buy` flag became true
  - `BACK_IN_STOCK` — `in_stock` flipped from false → true
  - `OOS` — `in_stock` flipped from true → false
- Assign severity (low / medium / high) based on discount depth

### F4 — CLI Interface
- Developer-friendly CLI for all operations:
  - `hd init-db`
  - `hd add-store <store_id>`
  - `hd discover [--brand] [--pages]`
  - `hd snapshot [--stores] [--limit]`
  - `hd run-once`
  - `hd alerts` (view recent alerts)

### F5 — Polite Crawling & Safety
- Max concurrency: 2–5 async requests
- Global rate: ≤ 1 req/sec (configurable)
- Random jitter: 200–800ms between requests
- Exponential backoff on 4xx/5xx errors
- Circuit breaker: pause crawling if error rate exceeds threshold

### F6 — Schema Drift Detection
- Monitor key JSONPaths in API response on every run
- If critical paths disappear across >X% of products, mark `source_health = DEGRADED`
- Stop crawling and emit a health alert

---

## 6. Non-Goals (v1)

- No CAPTCHA solving or anti-bot evasion tooling
- No high-frequency polling (not a real-time system)
- No user authentication or multi-user support
- No front-end UI (CLI only for v1)
- No coverage of stores beyond 2619 and 8425 in v1
- No "all SKUs ever listed" exhaustive coverage — best-effort discovery
- No pricing history charts or analytics UI

---

## 7. Constraints

- **Must not** violate rate limits or engage in aggressive scraping patterns
- **Must be** resilient to missing or renamed fields in API responses
- **Must preserve** raw API responses (optional file or JSONB) for debugging and schema replay
- **Must be** runnable locally on a single machine with no cloud dependencies in v1

---

## 8. Out-of-Scope for v1 / Future Consideration

- Email or Discord alert notifications (architecture should leave a clear hook for this)
- Additional brands (Makita, Ridgid, etc.)
- Additional stores beyond the initial two
- Price history trend analysis / visualizations
- FastAPI read-only dashboard endpoint (`hd serve`)
- Celery + Redis job queue for production scale

---

## 9. Assumptions

- Home Depot's internal GraphQL endpoint (`apionline.homedepot.com/federation-gateway/graphql`) remains accessible with browser-mimicking headers
- Store IDs 2619 and 8425 are valid and active
- The clearance navParam token `1z11adf` and Tools category navParam `N-5yc1vZc1xy` are stable enough for v1 discovery (drift detector will catch breakage)
- Brand name values `"Milwaukee"` and `"DEWALT"` (capitalized) are stable identifiers in the API response
