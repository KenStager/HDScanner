"""Environment checks for an installed scanner.

The test suite is strong on logic and blind on deployment. Two defects shipped
on 2026-08-20 lived entirely in that gap: a regenerated launchd plist that
resolved `hd` to an unrelated interpreter on PATH, and a prune agent that was
never registered at all — the second having quietly let 89% of the snapshot
table age past its retention window.

Neither is a code bug. Both are answerable by asking the machine a direct
question, which is what this module does.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hd.config import Settings

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def _agent_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _loaded_labels() -> set[str] | None:
    """Labels launchd currently knows about, or None if it cannot be asked."""
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    labels = set()
    for line in out.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if parts:
            labels.add(parts[-1].strip())
    return labels


def _plists() -> list[Path]:
    d = _agent_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*hdscanner*.plist") if p.suffix == ".plist")


def check_scheduler(settings: Settings) -> Iterable[Check]:
    """The scan job exists, is loaded, and runs the interpreter we think it does."""
    plists = [
        p for p in _plists()
        if not any(job in p.name for job in ("dashboard", "prune", "backup"))
    ]
    if not plists:
        yield Check("scheduler", FAIL, "no scan job installed",
                    "run `hd setup` to install the schedule")
        return

    labels = _loaded_labels()
    for path in plists:
        try:
            data = plistlib.loads(path.read_bytes())
        except (OSError, ValueError) as e:
            yield Check("scheduler", FAIL, f"{path.name} is unreadable: {e}")
            continue

        label = data.get("Label", "?")
        if labels is not None and label not in labels:
            yield Check("scheduler", FAIL, f"{label} is installed but not loaded",
                        f"launchctl load {path}")
        else:
            # launchd accepts a single dict or an array of dicts here.
            intervals = data.get("StartCalendarInterval") or []
            if isinstance(intervals, dict):
                intervals = [intervals]
            slots = [
                (d.get("Hour", 0), d.get("Minute", 0)) for d in intervals
            ]
            times = ", ".join(f"{h:02d}:{m:02d}" for h, m in sorted(slots))
            yield Check("scheduler", OK, f"{label} loaded — {times or 'no slots'}")

        yield from _check_interpreter(data, path)


def _check_interpreter(data: dict[str, Any], path: Path) -> Iterable[Check]:
    """The scheduled command must run this project's interpreter, not any `hd` on PATH.

    A bare `hd` in a login shell resolved to a different Python installation
    entirely, which fails silently: the job runs, and runs the wrong code.
    """
    args = data.get("ProgramArguments") or []
    command = args[-1] if args else ""
    if "hd " not in command and not command.endswith("hd"):
        yield Check("scheduler-command", WARN, "could not find an `hd` invocation")
        return

    import re

    tokens = re.findall(r"(\S*hd)\s+(?:run-once|notify|prune)", command)
    if not tokens:
        yield Check("scheduler-command", WARN, f"no recognised subcommand in {path.name}")
        return

    for token in set(tokens):
        if token == "hd":
            resolved = shutil.which("hd")
            yield Check(
                "scheduler-command", FAIL,
                f"{path.name} runs a bare `hd` (PATH resolves to {resolved or 'nothing'})",
                "re-render the plist with the absolute .venv/bin/hd path",
            )
        elif not Path(token).exists():
            yield Check("scheduler-command", FAIL, f"{token} does not exist",
                        "reinstall the schedule")
        elif ".venv" not in token:
            yield Check("scheduler-command", WARN,
                        f"{path.name} runs {token}, which is outside the project venv")
        else:
            yield Check("scheduler-command", OK, f"runs {token}")


def check_prune_job(settings: Settings) -> Iterable[Check]:
    """Nothing else deletes old snapshots; without this the database only grows."""
    prune = [p for p in _plists() if "prune" in p.name]
    if not prune:
        yield Check("prune-job", FAIL, "no prune job installed — snapshots are never deleted",
                    "run `hd setup` to install it, or `hd prune` by hand")
        return
    labels = _loaded_labels()
    for path in prune:
        try:
            label = plistlib.loads(path.read_bytes()).get("Label", "?")
        except (OSError, ValueError):
            yield Check("prune-job", FAIL, f"{path.name} is unreadable")
            continue
        if labels is not None and label not in labels:
            yield Check("prune-job", FAIL, f"{label} installed but not loaded",
                        f"launchctl load {path}")
        else:
            yield Check("prune-job", OK, f"{label} loaded")


def check_cooldown(settings: Settings) -> Iterable[Check]:
    from hd.http.cooldown import ThrottleCooldown

    cd = ThrottleCooldown(settings.throttle_cooldown_path)
    if cd.is_active():
        mins = round(cd.remaining_seconds() / 60)
        yield Check("throttle", WARN, f"in cooldown for another {mins} min — scans will defer")
    else:
        yield Check("throttle", OK, "no cooldown in force")


def check_liveness(settings: Settings) -> Iterable[Check]:
    from hd.pipeline.health import load_scan_health, outage_duration_hours

    state = load_scan_health(settings.health_state_path)
    now = datetime.now(timezone.utc)
    hours = outage_duration_hours(state, now)
    if state.status.value == "DEGRADED":
        span = f" for {hours:.0f}h" if hours else ""
        yield Check("liveness", FAIL,
                    f"scanning is degraded{span} "
                    f"({state.consecutive_failures} consecutive empty runs)")
    elif hours is None:
        yield Check("liveness", WARN, "no successful scan recorded yet")
    elif hours > 12:
        yield Check("liveness", WARN, f"last successful scan was {hours:.0f}h ago")
    else:
        yield Check("liveness", OK, f"last successful scan {hours:.0f}h ago")


def check_identity(settings: Settings) -> Iterable[Check]:
    # Report the User-Agent that actually goes on the wire, not the configured
    # one — a header policy can override it, and doctor must not claim an
    # identity the requests do not carry.
    from hd.http.client import build_headers, build_user_agent

    wire_ua = build_headers(settings).get("User-Agent", "")
    honest_ua = build_user_agent(settings)

    if wire_ua != honest_ua:
        yield Check("identity", WARN,
                    f"requests send '{wire_ua}', not the tool identity",
                    "requests do not carry the tool name or a contact address")
    elif settings.contact_email:
        yield Check("identity", OK, f"requests identify as {wire_ua}")
    else:
        yield Check("identity", WARN,
                    "no contact address on requests — nobody at Home Depot can reach you",
                    "set CONTACT_EMAIL in .env")


def check_curl(settings: Settings) -> Iterable[Check]:
    path = shutil.which("curl")
    if path:
        yield Check("curl", OK, path)
    else:
        yield Check("curl", FAIL, "curl not found — every API request goes through it")


def check_storage(settings: Settings) -> Iterable[Check]:
    """Raw responses are bounded by prune now; report only what is actually overdue."""
    import time

    raw = Path(settings.raw_json_dir)
    if not raw.is_dir():
        return

    files = list(raw.glob("*.json"))
    if not files:
        yield Check("raw-responses", OK, "empty")
        return

    total = sum(f.stat().st_size for f in files) / 1e6
    days = settings.raw_retention_days
    if days <= 0:
        yield Check("raw-responses", WARN,
                    f"{len(files):,} files, {total:,.0f} MB — retention disabled, grows forever",
                    "set RAW_RETENTION_DAYS to bound it")
        return

    cutoff = time.time() - days * 86400
    overdue = [f for f in files if f.stat().st_mtime < cutoff]
    if overdue:
        freed = sum(f.stat().st_size for f in overdue) / 1e6
        yield Check("raw-responses", WARN,
                    f"{len(files):,} files, {total:,.0f} MB — "
                    f"{len(overdue):,} past the {days}-day window",
                    "run `hd prune`")
    else:
        yield Check("raw-responses", OK,
                    f"{len(files):,} files, {total:,.0f} MB, none past {days} days")


async def check_database(settings: Settings) -> list[Check]:
    """Row counts, retention debt, and stores holding data we no longer scan."""
    from sqlalchemy import func, select

    from hd.db import base
    from hd.db.models import StoreSnapshot

    out: list[Check] = []
    try:
        await base.init_db(settings)
        async with base.get_session(settings) as session:
            total = (await session.execute(
                select(func.count()).select_from(StoreSnapshot))).scalar_one()
            cutoff = datetime.now(timezone.utc).timestamp() - settings.snapshot_retention_days * 86400
            cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
            stale = (await session.execute(
                select(func.count()).select_from(StoreSnapshot)
                .where(StoreSnapshot.ts < cutoff_dt))).scalar_one()
            rows = (await session.execute(
                select(StoreSnapshot.store_id, func.max(StoreSnapshot.ts))
                .group_by(StoreSnapshot.store_id))).all()
    except Exception as e:  # a broken database is itself the finding
        return [Check("database", FAIL, f"could not be read: {e}")]

    pct = (stale / total * 100) if total else 0
    out.append(Check(
        "retention", WARN if pct > 20 else OK,
        f"{total:,} snapshots, {stale:,} ({pct:.0f}%) past the "
        f"{settings.snapshot_retention_days}-day window",
        "run `hd prune`" if pct > 20 else None,
    ))

    configured = set(settings.store_list)
    retired = set(settings.retired_store_list)
    for store_id, last in sorted(rows):
        if store_id in configured or store_id in retired:
            continue
        out.append(Check(
            "stray-store", WARN,
            f"store {store_id} holds data but is not scanned (last seen {str(last)[:10]})",
            "if that is deliberate, add it to RETIRED_STORES",
        ))
    for store_id in sorted(configured - {r[0] for r in rows}):
        out.append(Check("store", WARN, f"store {store_id} is configured but has no data"))
    return out


async def check_walk_headroom(settings: Settings) -> list[Check]:
    """How close each walked node is to falling off the ceiling it rides on.

    A node that fits one walk is walked in one request stream. Grow it past the
    ceiling and the planner silently re-routes it: a single walk becomes a
    both-ends pair, and a both-ends pair becomes a multi-walk facet split. The
    planner handles all three correctly, so this is not an error — but it
    changes what a run costs, and for the in-store shelf it is the difference
    between "one walk, ~60 requests" and a facet split that may no longer fit
    the run it lives in. Nothing else reports the transition coming: the
    routing decision is a log line that scrolls away.

    Costs no API request. `walk_coverage.items_expected` is the node's own live
    total as of the last walk that read one, which is exactly the number the
    planner will compare against the ceiling next time.

    The margin is one seam's worth of overlap (`both_ends_min_overlap_pages`
    pages). That is not an arbitrary threshold: it is the slack the both-ends
    walk already holds back to keep its two ends overlapping, so a node inside
    it is one ordinary restock away from changing route.
    """
    from sqlalchemy import func, select

    from hd.db import base
    from hd.db.models import WalkCoverage
    from hd.pipeline.browse import both_ends_cap, reachable_cap

    # Every node's own latest measurement, chosen in SQL. An "ORDER BY started
    # DESC LIMIT n" would only see the last n rows, and a node on a slow
    # rotation can fall outside that window entirely — the check would then
    # report an affirmative all-clear over a set it had silently truncated,
    # which is the one thing this file exists not to do.
    try:
        await base.init_db(settings)
        async with base.get_session(settings) as session:
            newest = (
                select(WalkCoverage.store_id, WalkCoverage.tier, WalkCoverage.label,
                       func.max(WalkCoverage.started).label("started"))
                .where(WalkCoverage.items_expected.is_not(None))
                .group_by(WalkCoverage.store_id, WalkCoverage.tier, WalkCoverage.label)
                .subquery()
            )
            rows = (await session.execute(
                select(WalkCoverage.store_id, WalkCoverage.tier, WalkCoverage.label,
                       WalkCoverage.items_expected)
                .join(newest, (WalkCoverage.store_id == newest.c.store_id)
                      & (WalkCoverage.tier == newest.c.tier)
                      & (WalkCoverage.label == newest.c.label)
                      & (WalkCoverage.started == newest.c.started)))).all()
    except Exception as e:  # a database we cannot read is itself the finding
        return [Check("walk-headroom", FAIL, f"could not be read: {e}")]

    if not rows:
        return [Check("walk-headroom", OK, "no walk coverage recorded yet")]

    reach = reachable_cap(settings)
    band = both_ends_cap(settings) if settings.both_ends_paging else None
    margin = max(1, settings.both_ends_min_overlap_pages * settings.page_size)

    # store_id belongs in the key: two stores walk the same labels, and without
    # it one store's row masks the other's and the message names neither.
    latest: dict[tuple[str, str, str], int] = {}
    for store_id, tier, label, expected in rows:
        if expected:
            latest[(store_id, tier, label)] = expected

    out: list[Check] = []
    watched = 0
    for (store_id, tier, label), expected in sorted(latest.items()):
        if band is not None and reach < expected <= band:
            # Riding the both-ends band; the next stop is a facet split.
            headroom, ceiling = band - expected, band
            now, nxt = "one both-ends walk", "a multi-walk facet split"
            # band = 2*reachable_cap - overlap_pages*page_size, so LOWERING the
            # overlap widens the band. Saying "raise" here would push a node
            # straight over the edge this warning is about — at the cost of the
            # seam margin, which is why the trade is named rather than hidden.
            fix = ("lower BOTH_ENDS_MIN_OVERLAP_PAGES to widen the band (at the "
                   "cost of seam overlap), or budget for the extra walks")
        elif expected <= reach:
            # A plain single walk; the next stop is both-ends when enabled,
            # and a facet split when it is not.
            headroom, ceiling = reach - expected, reach
            now = "one walk"
            nxt = ("a both-ends pair (~2x this walk's requests)" if band is not None
                   else "a multi-walk facet split")
            fix = None if band is not None else "enable BOTH_ENDS_PAGING to absorb the growth"
        else:
            continue                                   # already past the ceiling
        watched += 1
        if headroom < margin:
            # The route changes at expected > ceiling, so it takes headroom+1
            # more items to trip it — not headroom. An operator reads this
            # number as literally true, so it has to be.
            out.append(Check(
                "walk-headroom", WARN,
                f"{label} ({tier}, store {store_id}) is {expected:,} of "
                f"{ceiling:,} — {headroom + 1} more item(s) turns {now} into {nxt}",
                fix,
            ))
    if not out:
        # Count only the nodes actually assessed. Nodes already past their
        # ceiling were skipped above and are not "clear of" anything.
        out.append(Check("walk-headroom", OK,
                         f"{watched} walked node(s) below a ceiling, all clear of it"))
    return out


async def check_scan_health(settings: Settings) -> list[Check]:
    """Is the scan job actually completing runs, and did any die mid-run?

    `scan_runs` records every run's lifecycle (running -> complete | aborted),
    but nothing reads it back: a hard crash leaves a row at 'running' forever —
    there is no heartbeat and no finalizer that could fire — and a launchd job
    that quietly stops firing leaves the newest completed run to age without
    any log line saying so. The liveness check reads the health-state file,
    which a crashed process never got to write; this one reads what the run
    itself durably recorded.

    Costs no API request. Thresholds derive from the configured schedule
    rather than assuming one: "stalled" means the newest completed run is
    older than two consecutive slot gaps, whatever the slots are.
    """
    from sqlalchemy import select

    from hd.db import base
    from hd.db.models import ScanRun

    try:
        await base.init_db(settings)
        async with base.get_session(settings) as session:
            rows = (await session.execute(
                select(ScanRun.id, ScanRun.started, ScanRun.status)
                .order_by(ScanRun.id))).all()
    except Exception as e:  # a database we cannot read is itself the finding
        return [Check("scan-health", FAIL, f"could not be read: {e}")]

    if not rows:
        return [Check("scan-health", OK, "no scan runs recorded yet")]

    now = datetime.now(timezone.utc)

    def age_hours(started: datetime) -> float:
        if started.tzinfo is None:  # SQLite stores these naive-UTC
            started = started.replace(tzinfo=timezone.utc)
        return (now - started).total_seconds() / 3600

    out: list[Check] = []

    # A run still 'running' an hour after start is a crashed process, not a
    # slow one: real runs finish in minutes, and the row can never repair
    # itself. Its walks went unrecorded, which downstream coverage readers
    # correctly treat as unknown-not-complete.
    for run_id, started, status in rows:
        if status == "running" and age_hours(started) > 1:
            out.append(Check(
                "scan-health", WARN,
                f"run {run_id} stuck at 'running' since {started:%Y-%m-%d %H:%M}Z "
                "— the process died mid-run; its walks are unrecorded",
                "check hd_launchd.stderr.log around that time",
            ))

    # Stalled: the newest completed run should never be older than two slot
    # gaps. Derived from the configured hours so a three-a-day install is not
    # held to a six-a-day clock.
    from hd.setup_schedule import SCAN_HOURS_ET as SHIPPED_HOURS
    hours = sorted(settings.scan_hours_et_list or SHIPPED_HOURS)
    max_gap = max(
        (hours[(i + 1) % len(hours)] - h) % 24 or 24
        for i, h in enumerate(hours)
    ) if len(hours) > 1 else 24
    threshold = 2 * max_gap
    completed = [(rid, st) for rid, st, status in rows if status == "complete"]
    if completed:
        newest_id, newest = max(completed, key=lambda r: r[1])
        age = age_hours(newest)
        if age > threshold:
            out.append(Check(
                "scan-health", WARN,
                f"newest completed run ({newest_id}) is {age:.1f}h old — more "
                f"than two slot gaps ({threshold}h); the record is not advancing",
                "check the launchd job is loaded and hd_launchd.stderr.log",
            ))
    elif age_hours(rows[0].started) > threshold:
        out.append(Check(
            "scan-health", WARN,
            f"{len(rows)} run(s) recorded, none ever completed",
            "check hd_launchd.stderr.log",
        ))

    # A tail of aborted runs is the throttle biting run after run — each one
    # individually logged, the streak never surfaced.
    streak = 0
    for _, _, status in reversed(rows):
        if status == "aborted":
            streak += 1
        elif status == "complete":
            break
    if streak >= 3:
        out.append(Check(
            "scan-health", WARN,
            f"the last {streak} finished runs all aborted — sustained "
            "throttling or a recurring failure, not a one-off",
            "check request budgets and the cooldown state",
        ))

    if not out:
        detail = (
            f"{len(rows)} run(s) recorded; newest completed {age:.1f}h ago"
            if completed else
            f"{len(rows)} recent run(s) recorded, none completed yet"
        )
        out.append(Check("scan-health", OK, detail))
    return out


# Below this share of a node's own total, a truncated walk missed something
# that matters. At or above it, the shortfall is the node's live total moving
# under the walk — catalog churn, not a collection failure.
COVERAGE_CHURN_FLOOR = 0.95


async def check_coverage_quality(settings: Settings) -> list[Check]:
    """Does "truncated" mean churn, or does it mean we actually missed a node?

    `walk_status` marks a walk truncated whenever it saw fewer items than the
    node's own live total — including when it paged to a clean stop and the
    total simply moved underneath it. That is deliberate under-claiming and the
    safe direction. But it puts two unrelated quantities behind one word, and
    the aggregate complete:truncated ratio therefore cannot be read: a walk
    that missed 6 of 253 items and a walk that missed 1,551 of 2,197 are the
    same row to it.

    This check does the division the ratio cannot. Both numbers already exist
    in the record — `items_expected` and `items_observed` — so nothing new is
    stored; what was missing was anyone computing them.

    Scope, which is load-bearing and has been wrong twice:

    A doctor reports CURRENT health, and `walk_coverage` is append-only across
    walk shapes that no longer exist. Summing it whole reported a node retired
    days ago as a live problem and counted one chronically short node once per
    run it ever ran in — measured 9x too high on this install. So the rows are
    reduced to the latest walk per node.

    The reduction takes the latest walk of ANY status and only then keeps the
    truncated ones. Filtering to truncated first makes "latest" mean "latest
    TRUNCATED walk", so a node that has since completed keeps reporting the
    truncation it already recovered from. That was the second error, and it
    named a healthy node (complete, 141/141) as the install's worst offender.

    The key is `(store_id, tier, nav_param)`, not `nav_param` alone.
    `nav_param` is the only STABLE identity a node has — `label` is a display
    string that can change between runs — but it is not a UNIQUE one: it is
    composed from the catalog root plus facet tokens, so the same value names
    the same region at every store and under either storefilter. Keyed on the
    node alone, one store's walk silently overwrites another's.

    Rows with no `nav_param` cannot be attributed to a node at all, so they
    can be nobody's "latest"; they are counted and named separately rather
    than folded into either bucket or dropped.

    Costs no API request.
    """
    from sqlalchemy import select

    from hd.db import base
    from hd.db.models import WalkCoverage

    try:
        await base.init_db(settings)
        async with base.get_session(settings) as session:
            all_rows = (await session.execute(
                select(WalkCoverage.label,
                       WalkCoverage.items_expected,
                       WalkCoverage.items_observed,
                       WalkCoverage.nav_param,
                       WalkCoverage.store_id,
                       WalkCoverage.tier,
                       WalkCoverage.status,
                       WalkCoverage.started)
                .order_by(WalkCoverage.started))).all()
    except Exception as e:  # a database we cannot read is itself the finding
        return [Check("coverage-quality", FAIL, f"could not be read: {e}")]

    # Reduce to the latest walk of EVERY status first, and only then keep the
    # truncated ones. Filtering to truncated before reducing would make
    # "latest" mean "latest TRUNCATED walk", so a node that has since
    # completed would keep reporting the truncation it recovered from — the
    # same staleness this scoping exists to remove, one step over.
    #
    # The key is (store, tier, node), not the node alone: nav_param is
    # composed from the catalog root plus facet tokens, so the same value
    # names the same region at every store and under either storefilter.
    # Keying on it alone would let one store's walk overwrite another's.
    latest: dict[tuple[str, str, str], tuple[str, int | None, int, str]] = {}
    for label, expected, observed, nav, store_id, tier, status, _started in all_rows:
        if nav:
            latest[(store_id, tier, nav)] = (label, expected, observed, status)

    rows = [(label, expected, observed)
            for (label, expected, observed, status) in latest.values()
            if status == "truncated"]

    # Counted from the truncated rows only, and never reduced: a row with no
    # nav_param cannot be attributed to a node, so it cannot be anyone's
    # "latest" either.
    unattributable = sum(
        1 for _, _, _, nav, _, _, status, _ in all_rows
        if status == "truncated" and not nav
    )

    # A truncated walk with no denominator cannot be judged either way. Counted
    # and reported, never silently folded into the healthy bucket.
    unjudgeable = sum(1 for _, exp, _ in rows if not exp or exp <= 0)
    judgeable = [(lab, exp, obs) for lab, exp, obs in rows if exp and exp > 0]

    if not judgeable:
        # Blind rows are not a clean bill of health. Reporting OK here while
        # the SAME rows raise a warning as soon as one judgeable walk appears
        # would invert the check on its own axis.
        if unjudgeable:
            out = [Check(
                "coverage-quality", WARN,
                f"{unjudgeable} truncated walk(s) recorded no expected total, "
                "so no shortfall can be judged in either direction",
            )]
        else:
            out = [Check("coverage-quality", OK, "no truncated walks recorded")]
        if unattributable:
            out.append(_unattributable_check(unattributable))
        return out

    churn = [(lab, exp, obs) for lab, exp, obs in judgeable
             if obs / exp >= COVERAGE_CHURN_FLOOR]
    material = [(lab, exp, obs) for lab, exp, obs in judgeable
                if obs / exp < COVERAGE_CHURN_FLOOR]

    # A walk can observe MORE than the node claimed when the total grows
    # mid-walk (walk_status still calls that truncated if the walk was cut).
    # Clamping at zero keeps a negative shortfall from silently crediting
    # itself against a real loss elsewhere in the sum.
    churn_missed = sum(max(0, exp - obs) for _, exp, obs in churn)
    material_missed = sum(max(0, exp - obs) for _, exp, obs in material)

    out: list[Check] = []
    if material:
        worst = sorted(material, key=lambda r: r[2] / r[1])[:3]
        worst_txt = ", ".join(
            f"{lab.rsplit('/', 1)[-1]} {obs}/{exp}" for lab, exp, obs in worst
        )
        out.append(Check(
            "coverage-quality", WARN,
            f"{len(material)} truncated walk(s) missed real coverage "
            f"({material_missed:,} items); {len(churn)} missed only churn "
            f"({churn_missed:,} items). Worst: {worst_txt}",
            "the headline truncation ratio counts both alike — read this split "
            "instead, and check the worst nodes against the reachable ceiling",
        ))
    else:
        out.append(Check(
            "coverage-quality", OK,
            f"all {len(churn)} truncated walk(s) are churn-level "
            f"(≥{COVERAGE_CHURN_FLOOR:.0%} seen, {churn_missed:,} items total)",
        ))

    if unjudgeable:
        out.append(Check(
            "coverage-quality", WARN,
            f"{unjudgeable} truncated walk(s) recorded no expected total, so "
            "their shortfall cannot be judged in either direction",
        ))
    if unattributable:
        out.append(_unattributable_check(unattributable))
    return out


def _unattributable_check(count: int) -> Check:
    """Truncated walks with no nav_param cannot be tied to a node.

    They are history — written before the column existed, or by a walk shape
    since retired — so they say nothing about any node's current state. Named
    rather than dropped: silently discarding rows is how a metric starts
    lying, which is the thing this check exists to stop.
    """
    return Check(
        "coverage-quality", OK,
        f"{count} older truncated walk(s) carry no nav_param and are not "
        "attributable to a current node; excluded from the split above",
    )


async def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    for fn in (check_scheduler, check_prune_job, check_liveness, check_cooldown,
               check_identity, check_curl, check_storage):
        try:
            checks.extend(fn(settings))
        except Exception as e:
            checks.append(Check(fn.__name__, FAIL, f"check itself failed: {e}"))
    try:
        checks.extend(await check_database(settings))
    except Exception as e:
        checks.append(Check("database", FAIL, f"check itself failed: {e}"))
    try:
        checks.extend(await check_walk_headroom(settings))
    except Exception as e:
        checks.append(Check("walk-headroom", FAIL, f"check itself failed: {e}"))
    try:
        checks.extend(await check_scan_health(settings))
    except Exception as e:
        checks.append(Check("scan-health", FAIL, f"check itself failed: {e}"))
    try:
        checks.extend(await check_coverage_quality(settings))
    except Exception as e:
        checks.append(Check("coverage-quality", FAIL, f"check itself failed: {e}"))
    return checks


# --- repair ------------------------------------------------------------------
#
# A checker that only ever prints advice leaves the problem exactly where it
# found it. These are the repairs that are safe to make without asking: they
# install missing scheduling, and they do not delete anything. Anything
# destructive stays a suggestion.


async def apply_fixes(settings: Settings, checks: list[Check]) -> list[str]:
    """Repair what can be repaired safely. Returns a line per action taken."""
    actions: list[str] = []
    names = {c.name for c in checks if c.status in (FAIL, WARN)}

    if "prune-job" in names:
        actions.append(await _install_prune_job(settings))

    if "retention" in names:
        actions.append(
            "retention: not repaired automatically — deleting rows is your call. "
            "Run `hd prune --dry-run`, then `hd prune`."
        )
    if "raw-responses" in names:
        files, freed = _prune_raw(settings)
        if files:
            actions.append(
                f"raw-responses: removed {files:,} file(s), freed {freed/1e6:,.0f} MB"
            )
    return [a for a in actions if a]


def _prune_raw(settings: Settings) -> tuple[int, int]:
    """Delete expired raw responses. Safe to automate: they are regenerated each run."""
    from hd.cli import prune_raw_responses

    return prune_raw_responses(settings)


async def _install_prune_job(settings: Settings) -> str:
    """Render and load the maintenance job that should already have existed."""
    from hd.setup_schedule import (
        hd_executable,
        is_macos,
        label_for,
        load_agent,
        launch_agents_dir,
        prune_slot,
        render_prune_plist,
        write_agent,
    )

    if not is_macos():
        return "prune-job: not installed — launchd is macOS only; add a cron entry instead"

    hd = hd_executable()
    if hd is None or not Path(hd).exists():
        return "prune-job: could not locate the hd executable to schedule"

    label = f"{label_for()}.prune"
    path = launch_agents_dir() / f"{label}.plist"
    slot = prune_slot()
    write_agent(path, render_prune_plist(label, Path.cwd(), Path(hd), slot))
    ok, output = await load_agent(path)
    if not ok:
        return f"prune-job: wrote {path.name} but launchctl refused it — {output}"
    return f"prune-job: installed and loaded {label} (daily at {slot.hour:02d}:{slot.minute:02d})"
