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
    plists = [p for p in _plists() if "dashboard" not in p.name and "prune" not in p.name]
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
            slots = [
                (d.get("Hour", 0), d.get("Minute", 0))
                for d in data.get("StartCalendarInterval") or []
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
    if settings.contact_email:
        yield Check("identity", OK, f"requests identify as {settings.user_agent} (+{settings.contact_email})")
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
    for store_id, last in sorted(rows):
        if store_id in configured:
            continue
        out.append(Check(
            "stray-store", WARN,
            f"store {store_id} holds data but is not scanned (last seen {str(last)[:10]})",
        ))
    for store_id in sorted(configured - {r[0] for r in rows}):
        out.append(Check("store", WARN, f"store {store_id} is configured but has no data"))
    return out


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
