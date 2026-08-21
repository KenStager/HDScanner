# HD Clearance Monitor

Watches specific Home Depot stores for clearance markdowns, price drops and
restocks on the brands you care about, and tells you when something genuinely
gets cheaper.

Clearance is a per-store event. A tool marked down at your store is often full
price two towns over, the discount rarely shows up in search, and it is gone
within days. This watches the stores you can actually drive to.

> Unofficial and unaffiliated with Home Depot. It reads the same public
> endpoints the website's own pages use, slowly, for personal use.

There is an illustrated walkthrough of all of this, with screenshots of the
dashboard, at **<https://www.kenstager.com/hdscanner/>**. This file is the
reference; that page is the guide.

**Contents** — [What you get](#what-you-get) · [Requirements](#requirements) ·
[Install](#install) · [Keep it running](#keep-it-running) ·
[Seeing your deals](#seeing-your-deals) · [Configuration](#configuration) ·
[Troubleshooting](#troubleshooting) · [How it works](#how-it-works) ·
[Development](#development)

---

## What you get

After a scan, `hd alerts` shows what changed at your stores:

```
                                 Recent Alerts
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┓
┃ Time             ┃ Store  ┃ Item      ┃ Type            ┃ Severity ┃ Details ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━┩
│ 2026-08-19 16:18 │ 1234   │ 342938293 │ CLEARANCE       │ medium   │ 41% off │
│ 2026-08-19 16:18 │ 1234   │ 342937828 │ CLEARANCE       │ medium   │ 45% off │
└──────────────────┴────────┴───────────┴─────────────────┴──────────┴─────────┘
```

It watches for `CLEARANCE`, `IN_STORE_CLEARANCE`, `PRICE_DROP`, `DEEP_DISCOUNT`,
`SPECIAL_BUY`, `BACK_IN_STOCK`, `OOS` and `PRICING_ERROR`, and raises
`HEALTH_DEGRADED` when the API itself starts misbehaving.

You can also have deals pushed to **Slack**, or browse them in a local **web
dashboard**. Both are optional — the command line works on its own.

Every deal is scored against price history the tool actually recorded, so it
tells you *"lowest in the 47 days I've been watching"* rather than implying a
saving it cannot back up.

---

## Requirements

| | |
|---|---|
| **Python** | 3.11 or newer. Check with `python3 --version` |
| **git** | To download the code. On a Mac that has never run developer tools you will be prompted to install them — that is normal, click **Install** and wait |
| **curl** | Required — every request goes through it. Preinstalled on macOS and most Linux |
| **A Home Depot near you** | Setup finds your stores from a ZIP code |
| **Time** | About 5 minutes to set up; the first full scan takes 10–30 minutes |
| **Slack** | Optional, for push alerts |

Runs on macOS and Linux. Scheduling is automatic on macOS (launchd); on Linux
setup prints crontab lines for you to paste.

---

## Install

Run these one line at a time.

```bash
git clone https://github.com/KenStager/HDScanner.git
cd HDScanner
ls
```

**Stop and check:** that `ls` must list **pyproject.toml**. If it does not, the
download did not finish and everything after this will fail in a confusing way —
see [Troubleshooting](#troubleshooting). Only continue once you see it.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dashboard]"
```

Keep the quotation marks on that last line — without them the shell reads the
brackets as a filename pattern. It prints a lot of scrolling text; you want the
last line to say `Successfully installed`.

Your prompt should now start with `(.venv)`. **That lasts only as long as this
terminal window** — see [Keep it running](#keep-it-running).

Then run setup:

```bash
hd setup
```

It asks two things you already know — a **ZIP code** and a **brand name** — and
finds everything else itself. Setup is a conversation: it asks one question at a
time and waits. **Nothing below is typed by you** — it is a recording of what the
conversation looks like:

```
Where should the data live?
  Use PostgreSQL instead of SQLite? [y/N]:
  Connected to SQLite file ./dev.db
  Schema ready — 6 tables

Which stores should be watched?
  ZIP code: 78701

  #   Store                     Location        Distance
  1   SE Austin (6542)          Austin, TX        3.2 mi
  2   Austin Mueller (6892)     Austin, TX        3.3 mi
  3   South Austin (6570)       Sunset Valley     5.6 mi
  Select store(s), e.g. 1 or 1,3 [1]: 1

Which brands should be tracked?
  Brand name: Ryobi
  RYOBI -> token m5d (2,412 products)

Check it works?
  Working — 329 products, 329 snapshots recorded.
```

That transcript is shortened. The full sequence, and what to answer:

| It asks | You answer |
|---|---|
| Use PostgreSQL instead of SQLite? | **Enter** (no) |
| ZIP code | Your ZIP |
| Select store(s) | **Enter** for the closest, or `1,3` for two |
| Brand name | `Milwaukee`, `DEWALT`, `Ryobi` — whatever you are hunting |
| Add another brand? | **Enter** for no |
| Filters | **Enter** to skip |
| Set up Slack? | **Enter** to skip — you can add it later |
| Run a test scan? | **Enter** for yes — this is what proves it works |
| Install the schedule? | **Enter** for yes — this is what makes it run on its own |

In a prompt like `[y/N]`, the **capital letter is what you get if you just press
Enter**. Every answer above except the ZIP and the brand is the default.

A few steps pause for several seconds while they talk to Home Depot — the store
lookup, the brand check, and the test scan. That is work, not a freeze.

Setup is **re-runnable** and safe: it verifies before it writes. A brand that
would have scanned nothing is caught here rather than after a week of empty
runs. It also offers Slack and scheduling — both skippable, and skipping leaves
a fully working install.

Then take a first full scan:

```bash
hd run-once      # 10-30 minutes; paced deliberately, not hung
hd alerts        # see what it found
```

The first run has nothing to compare against, so it mostly records a baseline.
Real deal alerts start from the second run onward.

### Why setup exists

The two settings that matter are not guessable. Store ids are not published
anywhere obvious, and each brand needs an opaque catalog facet token — Milwaukee
is `zv` — that has no public lookup. Setting a brand without its token produces a
run that succeeds and scans nothing, which is exactly the failure setup prevents.

---

## Keep it running

**Every new terminal window starts fresh.** Before running any `hd` command:

```bash
cd ~/HDScanner
source .venv/bin/activate
```

You will see `(.venv)` appear at the start of your prompt. Without it, `hd` says
`command not found`. The scheduled background jobs do **not** need this — they
run `hd` by its full path — so this applies only to commands you type yourself.

Once the schedule and the dashboard are installed, you should rarely need a
terminal at all: the scans run themselves and the deals show up in your browser.

Clearance appears and disappears within days, so this is worth running on a
schedule. `hd setup` offers to install one; to do it later:

```bash
hd setup      # answer no to the steps you already have
```

You get three jobs:

- **Scan** — three times a day, eight hours apart, on a minute derived from
  your install so that everyone running this does not arrive at once. The
  04:00 Eastern slot lands just after the daily-deals refresh and checks it
  first, so no separate run is needed
- **Prune** — once a day, deleting snapshots past the retention window
- **Dashboard** — always on, if you installed the dashboard extra. It starts at
  login and comes back after a crash or a reboot, so your deal board is a URL
  that just works rather than something you have to start

Together they mean the terminal is an install tool, not a daily one: after
setup, everything reaches you through the dashboard or Slack.

Three passes a day is deliberate. Clearance persists for days rather than
hours, so a denser schedule finds substantially the same markdowns — and Home
Depot's request allowance is shared across everyone running this rather than
metered per install, so the shipped default is the only thing that scales. It
is tunable via `SCAN_HOURS_ET` if you have a reason.

Keep the prune job. Nothing else deletes old snapshots, and the database grows
without it. Your price history survives pruning — it lives in a separate
durable table.

---

## Seeing your deals

**Command line**

```bash
hd alerts --since 24          # last 24 hours
hd alerts --type CLEARANCE    # one kind
hd health                     # is the scanner healthy?
```

**Web dashboard** — a deal board per store, today's daily deals checked against
the prices this install actually recorded, a product browser with price charts,
and the alert feed:

If setup installed the dashboard job it is already running — open
<http://127.0.0.1:8080> and leave it bookmarked. To start one by hand:

```bash
hd serve                      # → http://127.0.0.1:8080
```

**Slack** — see [Slack setup](#slack) below.

---

## Configuration

`hd setup` writes `.env` for you. Edit it directly if you prefer. Defaults are
deliberately empty, so a fresh clone never scans someone else's neighbourhood.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./dev.db` | SQLite file, or `postgresql+asyncpg://…` with `pip install -e ".[postgres]"` |
| `STORES` | *(empty)* | Comma-separated store ids |
| `BRANDS` | *(empty)* | Comma-separated brand names |
| `BRAND_TOKENS` | *(empty)* | `Brand:token` pairs — browse walks **only** these |
| `PRODUCT_LINE_FILTERS` | *(empty)* | Optional title/model filters, e.g. `M12,M18` |
| `RATE_LIMIT_RPS` | `0.5` | Requests per second — see [Being a good citizen](#being-a-good-citizen) |
| `SNAPSHOT_RETENTION_DAYS` | `90` | How long raw snapshots are kept before pruning |
| `SLACK_BOT_TOKEN` | *(empty)* | Bot token (`xoxb-…`) |
| `SLACK_CHANNEL_ID` | *(empty)* | Channel to post to, e.g. `C0123456789` |
| `CANVAS_ENABLED` | `true` | Live deal-rundown canvas (paid Slack workspaces only) |

`BRANDS` and `BRAND_TOKENS` belong together — setup always writes both. Adding a
brand by hand without its token makes browse skip it silently. Every setting
lives in `src/hd/config.py`.

### Slack

Setup configures this for you. By hand:

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps)
2. Add the **`chat:write`** bot scope (and `canvases:write` for the deal canvas)
3. Install it and copy the **Bot User OAuth Token** (`xoxb-…`)
4. **Invite the bot to your channel:** `/invite @your-app`

Step 4 is the one everyone misses. Without it Slack rejects posts with
`not_in_channel`, which looks like a broken token.

```bash
hd notify --dry-run    # print what would be sent
hd notify              # send new alerts since last run
```

Free Slack workspaces cannot create standalone canvases. Alerts work regardless;
set `CANVAS_ENABLED=false` to silence the attempt.

---

## Troubleshooting

**Start here:**

```bash
hd doctor          # what is wrong
hd doctor --fix    # repair what can be repaired safely
```

It checks the things that break silently — a schedule that stopped firing, a
prune job that was never registered, an `hd` that resolves to the wrong Python,
a degraded API, retention debt, curl — and says which. `--fix` reinstalls
missing jobs and clears junk; it never deletes price history, and anything
destructive stays a suggestion rather than an action.

The same checks appear as a banner across the top of the dashboard, so an
install nobody is watching still says when it stopped collecting.

**"does not appear to be a Python project: neither 'setup.py' nor 'pyproject.toml' found"**
You are not in the project folder. `pip install -e .` installs whatever is in the
*current* directory, and the download step did not finish. Start over:

```bash
cd ~
git clone https://github.com/KenStager/HDScanner.git
cd HDScanner
ls          # must list pyproject.toml
```

If `git clone` did nothing, or said `command not found: git`, run
`xcode-select --install` first, let it finish, then clone again.

**"requires a different Python: 3.9 … not in '>=3.11'"**
Your `python3` is older than 3.11. Install a current Python from
[python.org](https://www.python.org/downloads/), open a **new** terminal window,
confirm `python3 --version`, then delete the `.venv` folder and redo the install
steps.

**`zsh: no matches found: .[dashboard]`**
The quotation marks were dropped. It is `pip install -e ".[dashboard]"`.

**`command not found: hd`**
The virtual environment is not active in this window. See
[Keep it running](#keep-it-running).

**"No stores configured. Run `hd setup` first."**
Expected on a fresh clone — the defaults are empty on purpose. Run `hd setup`.

**No Home Depot found near my ZIP**
Setup widens to 50 then 100 miles automatically. If nothing turns up, the ZIP
may not be one Home Depot recognises — try a neighbouring one.

**"No brand called 'X'"**
The name must match Home Depot's own catalog spelling. Setup suggests close
matches. Case does not matter; `dewalt` finds `DEWALT`.

**A run says "coverage incomplete"**
Home Depot throttled it (HTTP 206). Normal for a burst, and the scheduled runs
are paced. Nothing is lost — the next run resumes where this one stopped.

**Slack says `not_in_channel`**
Invite the bot: `/invite @your-app` in the target channel.

**`hd serve` fails to start**
Install the dashboard extra: `pip install -e ".[dashboard]"`.

**The database is getting large**
`hd doctor` reports whether the prune job is registered and how much retention
debt has built up; `hd doctor --fix` installs the job if it is missing. To
clear the backlog now: `hd prune --dry-run`, then `hd prune`. If prune refuses,
run `hd backfill-stats` first — it is protecting price history that exists
nowhere else.

**Alerts stopped appearing**
`hd doctor` covers the usual causes — a stopped schedule, a degraded API, a
cooldown after throttling. `hd health` reports the last run's status alone.

---

## How it works

Each run walks the brand's own category facets — the data behind the website's
left-hand navigation — rather than running keyword searches. Keyword search
misses clearance structurally: it excludes brand items outside the searched
category, and the API refuses to page past 720 results, so large sets cannot be
read to the end.

Two tiers:

- **shelf** (`storefilter=IN_STORE`) — everything physically stocked at your
  store, swept fully every run
- **network** (`storefilter=ALL`) — the full brand catalog, which also carries
  ship-to-store clearance the in-store filter hides. Categories rotate across
  runs so each is covered completely when its turn comes

Both tiers record products and prices from the same pages, so a newly discovered
item is price-monitored the same run it first appears. Categories above the API's
reachable ceiling are split by subcategory, then by price bracket; anything still
unreachable is reported as truncated rather than silently skipped.

That last point is the design rule throughout: **a run that could not see
everything says so.** A scan is never allowed to look successful when it was
throttled, truncated, or scanning nothing.

### Data

| Table | Role |
|---|---|
| `products` | Every item seen, with brand, title and model |
| `store_snapshots` | Append-only price/stock readings. Pruned at retention age |
| `item_price_stats` | Durable price facts — **survives pruning** |
| `alerts` | What was raised, when, and why |
| `stores`, `dismissed_deals` | Watched stores; deals you have dismissed |

`store_snapshots` is raw and disposable; `item_price_stats` is the record that
lasts. Never rebuild the aggregate from snapshots in normal operation — the rows
it came from may already be gone.

---

## Being a good citizen

This reads Home Depot's own endpoints at a deliberately unhurried pace.
`RATE_LIMIT_RPS` defaults to 0.5 — a courtesy floor, not a tuning knob. Raising
it does not find more deals: coverage is bounded by a request budget and
category rotation, not by how fast it asks.

Home Depot signals throttling with **HTTP 206 and an empty body**, not 429. The
client latches that, abandons the run and reports it, rather than reading a
truncated result as the end of the catalog.

Run one instance, against your own stores. There is no proxy rotation, CAPTCHA
solving or bot evasion here, and adding any would change what this is.

---

## Development

```bash
pip install -e ".[dev]"
pytest                        # 807 tests
```

```
cli.py                        typer commands
doctor.py                     hd doctor — deployment checks and safe repairs
setup_wizard.py               hd setup — the interactive first-run flow
  setup_database.py             provisioning: SQLite or PostgreSQL
  setup_slack.py                token, channel and scope verification
  setup_schedule.py             launchd / crontab generation (scan, prune, dashboard)
pipeline/
  browse.py                   facet-driven scan (the default strategy)
  brands.py                   brand name → catalog facet token
  diff.py                     snapshots → alerts
  snapshot.py, discovery.py   legacy keyword scan path
hd_api/
  stores.py                   ZIP → store lookup
  graphql.py, parsers.py      requests and response normalisation
http/client.py                retry, rate limit, circuit breaker, throttle cooldown
db/                           SQLAlchemy models, engine, price-stat folding
notifiers/                    Slack message and canvas delivery
dashboard/                    NiceGUI web UI
```

Architecture notes for contributors and coding agents live in `AGENTS.md`.
`PRD.md`, `SPEC.md` and `TASKS.md` are the original design documents, kept as a
historical record.

### Safety properties

- Rate limiting, jitter and backoff are always on
- A circuit breaker halts scanning when the error rate climbs
- Schema drift raises `HEALTH_DEGRADED` rather than failing quietly
- `store_snapshots` is append-only — history is never rewritten
- Destructive operations refuse to run when price history is uncaptured

---

## License

MIT — see [LICENSE](LICENSE). Use it, change it, redistribute it; no warranty.
