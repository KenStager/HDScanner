# HD Clearance Monitor

A backend CLI tool that monitors Milwaukee and DeWalt tool products at Home Depot stores for clearance events, price drops, and inventory changes. It replicates the internal GraphQL API calls that homedepot.com makes in the browser, stores historical snapshots, and generates actionable alerts.

## Quick Start

```bash
# Install (Python 3.11+)
pip install -e ".[dev]"

# Initialize database and seed stores
hd init-db

# Run the full pipeline (discover products, snapshot prices, diff, generate alerts)
hd run-once

# View recent alerts
hd alerts --since 24
```

## Configuration

All settings are read from `.env` (or environment variables). Copy `.env` and fill in your values:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./dev.db` | Database connection string |
| `STORES` | `2619,8425` | Comma-separated store IDs to monitor |
| `BRANDS` | `Milwaukee` | Comma-separated brand names |
| `PRODUCT_LINE_FILTERS` | `M12,M18` | Product line prefixes to match in titles |
| `MAX_PAGES` | `10` | Max API pages per discovery run |
| `RATE_LIMIT_RPS` | `1.0` | Max requests per second |
| `OPENCLAW_WEBHOOK_URL` | *(empty)* | OpenClaw webhook endpoint for Slack delivery |
| `OPENCLAW_TOKEN` | *(empty)* | `x-openclaw-token` header value |
| `SLACK_CHANNEL_ID` | *(empty)* | Target Slack channel (e.g. `channel:C1234567890`) |
| `NOTIFY_CURSOR_PATH` | `.hd_notify_cursor` | File path for notification dedup cursor |

See `src/hd/config.py` for the full list of settings.

## CLI Commands

```
hd init-db                             Create/migrate tables, seed configured stores
hd add-store <id> [--name] [--state]   Add a store to the database
hd discover [--brand] [--pages]        Populate products table from HD API
hd snapshot [--stores] [--limit]       Fetch pricing/inventory snapshots
hd run-once                            Full pipeline: discover + snapshot + diff + alerts
hd alerts [--limit] [--type] [--since] Print recent alerts
hd notify [--dry-run] [--reset]        Send alerts to Slack via OpenClaw webhook
hd health                              Print last run health status
hd prune [--days] [--dry-run]          Delete old snapshots beyond retention period
hd serve [--host] [--port]             Start NiceGUI web dashboard (requires dashboard extra)
```

## Slack Notifications via OpenClaw

The `hd notify` command sends recent alerts to Slack through an OpenClaw webhook endpoint. It's designed to run as a cron step after `hd run-once`.

### Setup

1. Set the OpenClaw webhook URL and (optionally) auth token in `.env`:

   ```
   OPENCLAW_WEBHOOK_URL=http://127.0.0.1:18789/hooks/agent
   OPENCLAW_TOKEN=your-token-here
   SLACK_CHANNEL_ID=channel:C1234567890
   ```

2. Test with a dry run:

   ```bash
   hd notify --dry-run
   ```

3. Add to cron:

   ```bash
   # Every 4 hours
   0 */4 * * * cd /path/to/HD_crawler && hd run-once && hd notify
   ```

### How It Works

- **Cursor-based dedup**: A cursor file (`.hd_notify_cursor`) stores the timestamp of the last successfully notified alert. Each run only sends alerts newer than the cursor. This is self-correcting regardless of cron timing drift.
- **Grouping**: Alerts for the same item and type within a 10-minute window are grouped together. A price drop detected at both stores 2619 and 8425 shows as one alert with both stores listed.
- **Filtering**: `HEALTH_DEGRADED` alerts are excluded from Slack (they're internal monitoring signals).
- **Webhook payload**: POSTs JSON to the OpenClaw endpoint:

  ```json
  {
    "message": "<Slack mrkdwn formatted text>",
    "deliver": true,
    "channel": "slack",
    "to": "<SLACK_CHANNEL_ID>"
  }
  ```

- **Non-fatal delivery**: Webhook failures are logged but never crash the pipeline. The cursor is only updated on successful delivery, so alerts will be retried on the next run.

### Options

| Flag | Description |
|---|---|
| `--since N` | Fallback: look back N hours if no cursor exists (default: 4) |
| `--dry-run` | Print the formatted Slack message to console without sending |
| `--reset` | Delete the cursor file and re-send from `--since` window |

### Example Output

```
🏷️ *PRICE_DROP* (high) — Milwaukee M18 FUEL 1/2 in. Drill Driver Kit
Stores: 2619, 8425
$299.00 → $249.00 (-17%)
Stock: In Stock / 3 units (2619), In Stock / 1 unit (8425)
<https://www.homedepot.com/p/315442497|View on HomeDepot.com>
```

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
          └── webhook.py      → curl-based OpenClaw webhook delivery
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

## Safety

- Rate limiting, jitter, and backoff are always active
- Circuit breaker pauses crawling if error rate exceeds threshold
- Schema drift detection emits `HEALTH_DEGRADED` alerts on API changes
- No proxy rotation, CAPTCHA solving, or bot evasion
- `store_snapshots` table is append-only — historical data is never modified
