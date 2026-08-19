# HD Clearance Monitor

Tracks clearance events, price drops and stock changes for the brands you care
about at the Home Depot stores you can actually drive to. It replicates the
GraphQL calls homedepot.com makes in the browser, keeps historical snapshots,
and alerts when something genuinely gets cheaper.

Clearance is a per-store event. A tool marked down at one store is often full
price two towns over, and the markdown is gone before it shows up anywhere
public — which is why this watches specific stores rather than a national feed.

## Quick Start

```bash
# Install (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,dashboard]"

# Answer two questions — a ZIP code and a brand — and it finds the rest
hd setup

# First full scan
hd run-once

# What turned up
hd alerts --since 24
```

`hd setup` takes an install from nothing to working: it provisions the database
(SQLite by default, or PostgreSQL — creating the database and schema for you),
finds your stores, resolves your brands, and optionally wires up Slack and a
schedule.

It exists because the settings that matter are not guessable. Store ids
are not published anywhere obvious, and each brand needs an opaque catalog
facet token (Milwaukee is `zv`) that has no public lookup. Setup finds your
stores from a ZIP code, resolves each brand name to its token and walks it to
confirm it returns products, then offers Slack delivery and a schedule. It is
re-runnable, and it verifies before it writes: a brand that would have scanned
nothing is caught during setup rather than after a week of empty runs.

## Configuration

Settings come from `.env`, which `hd setup` writes. Edit it directly if you
prefer; the defaults are deliberately empty so nothing scans a stranger's
neighbourhood out of the box.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./dev.db` | SQLite file, or `postgresql+asyncpg://…` with `pip install -e ".[postgres]"` |
| `STORES` | *(empty)* | Comma-separated store ids. `hd setup` fills this from a ZIP |
| `BRANDS` | *(empty)* | Comma-separated brand names |
| `BRAND_TOKENS` | *(empty)* | `Brand:token` pairs. Browse walks **only** these — a brand without one scans nothing |
| `PRODUCT_LINE_FILTERS` | *(empty)* | Optional title/model filters, e.g. `M12,M18` |
| `RATE_LIMIT_RPS` | `0.5` | Requests per second. See Being a good citizen |
| `SLACK_BOT_TOKEN` | *(empty)* | Bot token (`xoxb-…`) for alerts |
| `SLACK_CHANNEL_ID` | *(empty)* | Channel to post to, e.g. `C0123456789` |
| `CANVAS_ENABLED` | `true` | Live deal rundown canvas. Free Slack workspaces cannot create one |
| `CANVAS_TITLE` | `Deal Rundown` | Heading on that canvas |

`BRANDS` and `BRAND_TOKENS` belong together. Setup always writes both; if you
edit by hand and add a brand without its token, browse mode will skip it
silently. See `src/hd/config.py` for every setting.

## CLI Commands

```
hd setup                               Interactive first-run setup (start here)
hd init-db                             Create/migrate tables, seed configured stores
hd add-store <id> [--name] [--state]   Add a store to the database
hd browse [--stores] [--tier]          Facet-driven brand browse: discover + snapshot by category
hd daily-deals [--force]               Price today's Daily Deals set for configured brands
hd discover [--brand] [--pages]        Populate products table from HD API (legacy keyword mode)
hd snapshot [--stores] [--limit]       Fetch pricing/inventory snapshots (legacy keyword mode)
hd run-once                            Full pipeline: browse (or discover+snapshot) + diff + alerts
hd alerts [--limit] [--type] [--since] Print recent alerts
hd notify [--dry-run] [--reset]        Send new alerts to Slack
hd health                              Print last run health status
hd prune [--days] [--dry-run]          Delete old snapshots beyond retention period
hd serve [--host] [--port]             Start NiceGUI web dashboard (requires dashboard extra)
```

## Browse Mode (default scan strategy)

`hd browse` (and `hd run-once` when `BROWSE_ENABLED=true`, the default) walks the
brand's own category facets instead of keyword searches. Keyword search misses
clearance deals structurally: HD's relevance excludes brand items outside the
scanned category (real missed deals lived in Plumbing and Garage), and the API
rejects `startIndex > 720`, so big result sets can't be paged to the end.

Browse mode reads the `dimensions` facet block (the website's left-nav data) to
get every category token with per-store counts, then walks each category:

- **shelf tier** (`storefilter=IN_STORE` = the BOPIS "Pick Up Today" facet):
  every item physically assorted to the store, swept fully each run.
- **network tier** (`storefilter=ALL`): the full brand set, which also carries
  ship-to-store (BOSS) clearance that IN_STORE hides. Categories rotate across
  runs via `BROWSE_CURSOR_PATH`; each is covered completely when its turn comes.

Both tiers upsert products and append snapshots from the same pages, so a newly
discovered item is price-monitored the same run it's first seen. Categories over
the 744-item API ceiling are split by subcategory facets, then price brackets;
anything still unreachable is logged as truncated — never silently skipped.

Config: `BRAND_TOKENS` (e.g. `Milwaukee:zv`, DEWALT is `4j2`), `ROOT_NAV_PARAM`,
`BROWSE_NETWORK_CATEGORIES_PER_RUN`, `BROWSE_REQUEST_BUDGET`.

## Slack Notifications

`hd setup` configures this, or set `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` by hand.

Create an app at [api.slack.com/apps](https://api.slack.com/apps), add the
`chat:write` bot scope, install it to your workspace, and copy the **Bot User
OAuth Token** (`xoxb-…`). Then invite the bot to the target channel:

```
/invite @your-app
```

That invite is the step everyone misses. Without it Slack rejects posts with
`not_in_channel`, which reads like a broken token.

```bash
hd notify              # send new alerts since the last run
hd notify --dry-run    # print what would be sent
hd notify --reset      # clear the dedup cursor and resend
```

Alerts are grouped so a single markdown post covers related items, and a cursor
file (`.hd_notify_cursor`) prevents re-sending. `hd run-once && hd notify` is
the pair the scheduled job runs.

### Deal rundown canvas (optional)

With the `canvases:write` scope, the monitor also maintains a Slack canvas
holding the current deals, rewritten in place on each run rather than posted
repeatedly. **Free Slack workspaces cannot create standalone canvases** — the
API refuses, alerts are unaffected, and you can turn it off with
`CANVAS_ENABLED=false`.


## Dashboard

Optional NiceGUI web dashboard with overview, product browser, alerts feed, and store summary pages.

```bash
pip install -e ".[dashboard]"
hd serve
# → http://127.0.0.1:8080
```

## Architecture

```
CLI (cli.py / typer)
  └── Pipeline orchestration
        ├── discovery.py      → calls GraphQL API → upserts products table
        ├── snapshot.py       → calls GraphQL API → appends store_snapshots
        ├── diff.py           → reads snapshots → produces Alert objects
        ├── alerts.py         → writes Alert objects to alerts table
        └── health.py         → checks API response health
              ↑
        hd_api/
          ├── graphql.py      → builds + sends GraphQL POST requests
          └── parsers.py      → maps raw JSON → NormalizedProduct / NormalizedSnapshot
              ↑
        http/
          ├── client.py       → curl subprocess wrapper with retry, circuit breaker
          └── rate_limit.py   → async token bucket + jitter
              ↑
        db/
          ├── base.py         → SQLAlchemy async engine + session
          └── models.py       → ORM models + enums
              ↑
        grouping.py           → alert grouping logic (shared by dashboard + notifiers)
        notifiers/
          ├── formatter.py    → Slack mrkdwn message formatting
          └── webhook.py      → curl-based Slack chat.postMessage delivery
```

## Database

- **SQLite** for local dev: `DATABASE_URL=sqlite+aiosqlite:///./dev.db`
- **PostgreSQL** for prod: `DATABASE_URL=postgresql+asyncpg://user:pass@host/db`

Tables: `products`, `stores`, `store_snapshots` (append-only), `alerts`

## Testing

```bash
pip install -e ".[dev]"
pytest
```

197 tests covering parsers, diff engine, health checks, alert grouping, dashboard queries, formatters, and Slack notification formatting.

## Being a good citizen

This reads Home Depot's own endpoints at a deliberately unhurried pace.
`RATE_LIMIT_RPS` defaults to 0.5 — that is a courtesy floor, not a tuning knob.
Turning it up does not get you more deals; the scan is bounded by a request
budget and page rotation, not by how fast it asks.

Home Depot signals throttling with **HTTP 206 and a null body**, not 429. The
client latches that, abandons the run and reports it rather than treating a
truncated result as the end of the catalog — which is why a throttled run says
"coverage incomplete" instead of quietly claiming success.

Run one instance against your own stores. There is no proxy rotation, CAPTCHA
solving or bot evasion here, and adding some would change what this is.

## Safety

- Rate limiting, jitter, and backoff are always active
- Circuit breaker pauses crawling if the error rate exceeds a threshold
- Schema drift detection emits `HEALTH_DEGRADED` alerts on API changes
- `store_snapshots` is append-only — historical data is never modified
- `item_price_stats` is the durable record; snapshots are pruned at retention age
