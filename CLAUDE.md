# CLAUDE.md — Home Depot Clearance Monitor

This file is the primary context document for Claude Code. Read it in full before writing any code.

---

## What This Project Is

A backend Python CLI tool that monitors **Milwaukee and DeWalt tool products** at **Home Depot stores 2619 and 8425** for clearance events, price drops, and inventory changes. It works by replicating the internal GraphQL API calls that homedepot.com makes in the browser.

This is a **personal-use, self-hosted tool** with no user-facing UI in v1.

---

## Documents to Read First

Read these documents before starting any task:

1. `PRD.md` — What we are building and why
2. `SPEC.md` — Technical design, data models, API integration details
3. `TASKS.md` — Build order, task list, and acceptance gates (your primary work queue)
4. `compass_artifact_wf-1dda5335-81d8-4947-a52a-c05d03de3b39_text_markdown.md` — Standalone API research guide: full reverse-engineering of the Home Depot GraphQL endpoint, request/response schemas, clearance detection fields, inventory paths, navParam tokens, brand filter tokens, rate limiting notes, and community tooling survey

---

## Monitored Stores

| Store ID | Notes |
|---|---|
| `2619` | Primary store |
| `8425` | Secondary store |

These are the only stores for v1. Both must be pre-seeded during `hd init-db`. Do not hardcode them — they are set via `STORES` in the `.env` config.

---

## Key Rules — Read These Carefully

### Never hardcode configuration values

All store IDs, brand names, navparam tokens, API endpoints, rate limits, and directory paths must come from `config.py` which reads from `.env`. The only exception is the default values defined in `config.py` itself.

### Never inline GraphQL queries

The `searchModel` query lives in `queries/searchModel.graphql` and is loaded at runtime by `hd_api/graphql.py`. Do not paste the query string inside Python files.

### Parsers must be null-safe

Every field extraction from an API response must handle missing/null values gracefully. Use `response.get("key")` chains or wrap in try/except. A parser may return `None` for a field — it must never raise a `KeyError` or `TypeError` on a missing field.

### store_snapshots is append-only

Never write an `UPDATE` or `DELETE` statement against the `store_snapshots` table. Every fetch creates a new row. Historical data must be preserved.

### Be polite to the API

Rate limiting, jitter, and backoff are not optional — they are hardcoded requirements. The rate limiter must always be active. Never make concurrent requests beyond `MAX_CONCURRENCY`. Do not add any retry logic that could result in a rapid burst of requests.

### Do not attempt to bypass bot protection

No proxy rotation, no CAPTCHA solving, no headless browser emulation. If the API returns a 403, log a warning and back off. Do not try to circumvent it.

---

## Architecture Summary

```
CLI (cli.py / typer)
  └── Pipeline orchestration
        ├── discovery.py      → calls GraphQL API → upserts products table
        ├── snapshot.py       → calls GraphQL API → appends store_snapshots
        ├── diff.py           → reads snapshots → produces Alert objects
        ├── alerts.py         → writes Alert objects to alerts table
        └── health.py         → checks API response health → emits HEALTH_DEGRADED
              ↑
        hd_api/
          ├── graphql.py      → builds + sends GraphQL POST requests
          └── parsers.py      → maps raw JSON → NormalizedProduct / NormalizedSnapshot
              ↑
        http/
          ├── client.py       → httpx wrapper with headers, retry, circuit breaker
          └── rate_limit.py   → async token bucket + jitter
              ↑
        db/
          ├── base.py         → SQLAlchemy async engine + session
          └── models.py       → ORM models + enums
```

---

## Home Depot API Quick Reference

**Endpoint:** `POST https://apionline.homedepot.com/federation-gateway/graphql?opname=searchModel`

**Key custom headers (required):**
```
x-experience-name: general-merchandise
x-hd-dc: origin
x-debug: false
```

**Tools category navParam:** `N-5yc1vZc1xy`  
**Clearance filter token:** `1z11adf`  
**Combined:** `N-5yc1vZc1xyZ1z11adf`

**To detect clearance in a response:**
- `pricing.promotion.savingsCenter == "CLEARANCE"`
- `pricing.promotion.promotionTag == "Clearance"`
- `pricing.promotion.percentageOff` — the discount depth

**To get store-level inventory:**
- Navigate: `fulfillment.fulfillmentOptions[].services[].locations[]`
- Find location where `locationId == store_id`
- Read: `inventory.quantity`, `inventory.isInStock`, `inventory.isLimitedQuantity`, `inventory.isOutOfStock`

---

## Database Quick Reference

**Postgres (prod):** `DATABASE_URL=postgresql+asyncpg://...`  
**SQLite (dev):** `DATABASE_URL=sqlite+aiosqlite:///./dev.db`

Tables: `products`, `stores`, `store_snapshots` (append-only), `alerts`

Run migrations: `hd init-db`

---

## CLI Commands Quick Reference

```
hd init-db                            # create/migrate tables + seed stores 2619, 8425
hd add-store <id> [--name] [--state]  # add a store
hd discover [--brand] [--pages]       # populate products table
hd snapshot [--stores] [--limit]      # fetch + store pricing/inventory snapshots
hd run-once                           # full pipeline: discover+snapshot+diff+alerts
hd alerts [--limit] [--type] [--since]# print recent alerts
hd health                             # print last run health status
```

---

## Build Order

Follow `TASKS.md` in milestone order: **M0 → M1 → M2 → M3 → M4 → M5**

Do not skip ahead. Each milestone has an acceptance gate that must pass before the next begins.

---

## Testing Conventions

- Tests live in `tests/`
- Fixtures (saved API responses) live in `tests/fixtures/`
- Use `pytest-asyncio` for async tests
- Parser tests should use fixture JSON — not live API calls
- Diff tests should use constructed `NormalizedSnapshot` objects — not DB calls
- `conftest.py` should provide a test DB session fixture using SQLite in-memory

---

## What Does NOT Exist Yet

- No notification system (Discord/email hooks exist as config stubs only)
- No web dashboard (`hd serve` is a future milestone)
- No job queue (Celery/Redis is future)
- No multi-user support
- No stores beyond 2619 and 8425

---

## If The API Breaks

If `hd run-once` starts returning 0 results or getting 403s consistently:

1. Check `hd health` for `HEALTH_DEGRADED` status
2. Check `alerts` table for `HEALTH_DEGRADED` alert rows
3. Open browser DevTools → Network tab on homedepot.com and compare the request headers/body to what's in `graphql.py` and `SPEC.md §6`
4. Navparam tokens (`N-5yc1vZc1xy`, `1z11adf`) are the most likely to change — verify them by browsing the Tools/Clearance pages and extracting from the URL
5. Update `config.py` defaults or `.env` accordingly

---

## Code Style

- Type hints on all function signatures
- `async def` for all I/O operations (DB queries, HTTP requests)
- `structlog` for all logging — no bare `print()` statements in pipeline code (CLI output is fine)
- One responsibility per module — keep parsers pure (no DB), keep DB models free of business logic
- Short functions preferred — if a function exceeds ~40 lines, consider breaking it up
