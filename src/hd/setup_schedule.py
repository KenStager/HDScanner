"""Generate the scheduled jobs that keep the scanner running.

Two jobs, because they answer to different clocks. The scan runs several times
a day and once more just after Home Depot publishes its Daily Deals; the
maintenance job prunes old snapshots, which nothing else does — the shipped
schedule ran only `run-once` and `notify`, which is how a database reaches a
gigabyte and a half.

The Daily Deals slot is the reason this is not a fixed table of hours. Home
Depot refreshes that page at 3:00 Eastern, and launchd's StartCalendarInterval
fires on *local* time, so a hardcoded 3:10 reaches a Pacific user three hours
after the deals went up. The local equivalent is computed instead.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from xml.sax.saxutils import escape as xml_escape
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from hd.logging import get_logger

log = get_logger("setup_schedule")

HD_DEALS_TZ = ZoneInfo("America/New_York")
HD_DEALS_REFRESH = dtime(3, 0)
DEALS_GRACE_MINUTES = 10
# A dedicated deals slot is only worth a whole extra run when no routine scan
# already lands soon after the refresh. Every run checks the deals page anyway
# (run-once calls it before the browse tiers), so if a scan starts within this
# many hours of 3:00 ET the extra slot buys minutes of freshness at the cost of
# a full pipeline run — and that run competes for the same server-side
# allowance as the scan it precedes.
DEALS_COVERED_WITHIN_HOURS = 2

# The routine sweep, three times a day, eight hours apart. 04:00 exists because
# overnight repricing finishes by then: it and 12:00 are the full-shelf walks
# (see browse_full_shelf_hours_et), so both survive at this cadence.
#
# It used to be six. Nine of the last ten runs on the author's install ended on
# an HTTP 206 quota stop, which is the per-install rate limit already binding at
# a single install watching a single store. Clearance persists for days, not
# hours, so three passes find substantially the same markdowns — and the limit
# that matters is the one across every install, which does not aggregate.
SCAN_HOURS_ET = (4, 12, 20)
# Maintenance is placed in the middle of the quietest gap between scans rather
# than at a fixed hour. It used to sit at 04:30 — thirty minutes into the 04:00
# run, which is now the full-shelf walk and the longest of the day. Pruning
# deletes rows and may VACUUM, which takes an exclusive lock, so overlapping a
# scan means one of them loses.
PRUNE_HOUR_ET = 4  # retained for callers that want the historical default

DEFAULT_LABEL_BASE = "hdscanner"


@dataclass(frozen=True)
class ScheduleSlot:
    hour: int
    minute: int = 0


def _shell(path: Path | str) -> str:
    """Quote a path for /bin/bash. Spaces in a home directory are the norm on
    macOS, and an unquoted `cd` there fails with "too many arguments" — at
    04:00, into a log nobody reads."""
    return shlex.quote(str(path))


def _xml(value: Path | str) -> str:
    """Escape a value for XML text. An ampersand in a path — "Home & Garden" —
    otherwise produces a plist launchd refuses to load."""
    return xml_escape(str(value))


def _to_local(hour: int, minute: int, *, tz=None, on: datetime | None = None) -> ScheduleSlot:
    """Convert an Eastern wall-clock time to the machine's local wall clock.

    DST is resolved against a concrete date because the offset is not constant.
    Regions that shift on a different calendar than US Eastern will drift by an
    hour for a few weeks a year; that is a scheduling nicety, not a
    correctness problem, and beats being wrong by three hours all year.
    """
    reference = on or datetime.now()
    eastern = datetime.combine(reference.date(), dtime(hour, minute), tzinfo=HD_DEALS_TZ)
    local = eastern.astimezone(tz)
    return ScheduleSlot(local.hour, local.minute)


def daily_deals_slot(*, tz=None, on: datetime | None = None) -> ScheduleSlot:
    """Local time to catch Home Depot's Daily Deals just after they publish."""
    carry, minute = divmod(HD_DEALS_REFRESH.minute + DEALS_GRACE_MINUTES, 60)
    return _to_local(HD_DEALS_REFRESH.hour + carry, minute, tz=tz, on=on)


def deals_poll_slot(*, tz=None, on: datetime | None = None) -> ScheduleSlot:
    """Local time to start polling for the Daily Deals refresh.

    The poll job (`hd daily-deals --wait-for-refresh`) begins on the refresh
    itself rather than after a grace period: it re-reads the page until the
    set changes, so starting early costs one cheap read and starting late
    costs the minutes that a deal which sells out fast does not have.
    """
    return _to_local(HD_DEALS_REFRESH.hour, HD_DEALS_REFRESH.minute, tz=tz, on=on)


def deals_slot_needed(hours_et=SCAN_HOURS_ET) -> bool:
    """Whether the deals refresh needs a slot of its own.

    False when a routine scan already starts within DEALS_COVERED_WITHIN_HOURS
    of the 3:00 ET refresh — that scan checks the deals page before it does
    anything else, so a separate run would only duplicate it.
    """
    return not any(
        0 <= (h - HD_DEALS_REFRESH.hour) <= DEALS_COVERED_WITHIN_HOURS
        for h in hours_et
    )


def scan_minute(workdir: Path | None = None) -> int:
    """Which minute past the hour this install scans on.

    Derived from the install's own path rather than fixed at :00. A shared
    constant would put every install of this tool on Home Depot's doorstep at
    the same instant — a fixed offset like :15 only moves that crowd, it does
    not disperse it. Hashing the install path spreads them across the hour
    while staying stable for any one install, so the plist does not churn.
    """
    seed = str(workdir if workdir is not None else Path.cwd())
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 60


def scan_slots(
    *,
    tz=None,
    on: datetime | None = None,
    hours_et: Sequence[int] | None = None,
    minute: int | None = None,
    workdir: Path | None = None,
) -> list[ScheduleSlot]:
    """Every local time the scan should run, deals slot included, in order."""
    if hours_et is None:
        hours_et = _configured_hours()
    if minute is None:
        minute = _configured_minute(workdir)
    minute %= 60
    slots = [_to_local(h, minute, tz=tz, on=on) for h in hours_et]
    if deals_slot_needed(hours_et):
        slots.append(daily_deals_slot(tz=tz, on=on))
    unique = {(s.hour, s.minute) for s in slots}
    return [ScheduleSlot(h, m) for h, m in sorted(unique)]


def _configured_hours() -> Sequence[int]:
    """Settings override, else the shipped three-a-day cadence."""
    try:
        from hd.config import Settings

        hours = Settings().scan_hours_et_list
    except Exception:
        return SCAN_HOURS_ET
    return hours or SCAN_HOURS_ET


def _configured_minute(workdir: Path | None = None) -> int:
    """Settings override, else a minute derived from this install."""
    try:
        from hd.config import Settings

        configured = Settings().scan_minute
    except Exception:
        configured = None
    return scan_minute(workdir) if configured is None else configured


def quietest_hour_et(hours_et=SCAN_HOURS_ET) -> int:
    """Eastern hour furthest from any scan, for maintenance to run in.

    Picks the midpoint of the widest gap between consecutive scans, wrapping
    around midnight. With scans every four hours every gap is equal, so this
    settles on the first — two hours clear of the run before it and two hours
    clear of the run after.
    """
    hours = sorted(set(hours_et))
    if not hours:
        return PRUNE_HOUR_ET
    if len(hours) == 1:
        return (hours[0] + 12) % 24

    best_gap, best_hour = -1, hours[0]
    for current, following in zip(hours, hours[1:] + [hours[0] + 24]):
        gap = following - current
        if gap > best_gap:
            best_gap, best_hour = gap, (current + gap // 2) % 24
    return best_hour


def prune_slot(*, tz=None, on: datetime | None = None,
               hours_et: Sequence[int] | None = None) -> ScheduleSlot:
    """Local time for the maintenance job, in the quietest gap between scans.

    Computed from the cadence this install actually runs, not the shipped
    default: pruning takes an exclusive lock, so landing it inside a scan
    window means one of the two loses.
    """
    if hours_et is None:
        hours_et = _configured_hours()
    return _to_local(quietest_hour_et(hours_et), 30, tz=tz, on=on)


def hd_executable() -> Path | None:
    """Absolute path to the `hd` console script, or None if it cannot be found.

    Better than sourcing the virtualenv in the job: launchd runs with a bare
    environment, and an absolute path cannot be defeated by a missing shell
    profile or a renamed venv directory.

    Returns None rather than a guess when there is no `hd` beside the
    interpreter — under pipx, uv tool or `python -m hd` — so the caller can
    refuse to write a job that would fail silently at every scheduled time.
    """
    beside = Path(sys.executable).parent / "hd"
    if beside.exists():
        return beside
    found = shutil.which("hd")
    return Path(found) if found else None


def label_for(base: str = DEFAULT_LABEL_BASE, user: str | None = None) -> str:
    """A launchd label namespaced to the current user, not to a hardcoded one."""
    who = user or os.environ.get("USER") or "local"
    return f"com.{who}.{base}"


def _calendar_intervals(slots: list[ScheduleSlot]) -> str:
    return "\n".join(
        f"    <dict><key>Hour</key><integer>{s.hour}</integer>"
        f"<key>Minute</key><integer>{s.minute}</integer></dict>"
        for s in slots
    )


def render_scan_plist(
    label: str, workdir: Path, hd_path: Path, slots: list[ScheduleSlot]
) -> str:
    """launchd job for the recurring scan."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_xml(label)}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>export PYTHONUNBUFFERED=1; cd {_xml(_shell(workdir))} &amp;&amp; {_xml(_shell(hd_path))} run-once &amp;&amp; {_xml(_shell(hd_path))} notify</string>
  </array>

  <!-- Local times. One slot tracks Home Depot's 3:00 ET Daily Deals refresh,
       converted to this machine's timezone, because launchd fires on local
       time and a fixed 3:10 would miss it outside Eastern. -->
  <key>StartCalendarInterval</key>
  <array>
{_calendar_intervals(slots)}
  </array>

  <key>WorkingDirectory</key>
  <string>{_xml(workdir)}</string>

  <key>StandardOutPath</key>
  <string>{_xml(str(workdir) + "/hd_launchd.stdout.log")}</string>
  <key>StandardErrorPath</key>
  <string>{_xml(str(workdir) + "/hd_launchd.stderr.log")}</string>

  <!-- launchd catches up a missed run when the Mac wakes, so RunAtLoad is not
       needed to avoid gaps and would fire an extra scan on every login. -->
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
"""


def render_prune_plist(label: str, workdir: Path, hd_path: Path, slot: ScheduleSlot) -> str:
    """launchd job for retention. Nothing else deletes old snapshots."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_xml(label)}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>export PYTHONUNBUFFERED=1; cd {_xml(_shell(workdir))} &amp;&amp; {_xml(_shell(hd_path))} prune</string>
  </array>

  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>{slot.hour}</integer><key>Minute</key><integer>{slot.minute}</integer></dict>
  </array>

  <key>WorkingDirectory</key>
  <string>{_xml(workdir)}</string>

  <key>StandardOutPath</key>
  <string>{_xml(str(workdir) + "/hd_prune.stdout.log")}</string>
  <key>StandardErrorPath</key>
  <string>{_xml(str(workdir) + "/hd_prune.stderr.log")}</string>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""


def render_deals_poll_plist(label: str, workdir: Path, hd_path: Path, slot: ScheduleSlot) -> str:
    """launchd job that prices the Daily Deals set the minute it refreshes.

    Separate from the scan job on purpose: it reads one HTML page a few times
    and prices only the listed items of tracked brands, so it costs a handful
    of requests where a scan slot would cost a full pipeline run. The routine
    scan keeps its own sweep as the fallback and reports the set as already
    processed when this job got there first.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_xml(label)}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>export PYTHONUNBUFFERED=1; cd {_xml(_shell(workdir))} &amp;&amp; {_xml(_shell(hd_path))} daily-deals --wait-for-refresh &amp;&amp; {_xml(_shell(hd_path))} notify</string>
  </array>

  <!-- Local time for 3:00 Eastern, when Home Depot resets its Daily Deals.
       The job re-reads the page every couple of minutes until the set
       changes, then prices it, so the slot sits on the refresh, not after it. -->
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>{slot.hour}</integer><key>Minute</key><integer>{slot.minute}</integer></dict>
  </array>

  <key>WorkingDirectory</key>
  <string>{_xml(workdir)}</string>

  <key>StandardOutPath</key>
  <string>{_xml(str(workdir) + "/hd_dailydeals.stdout.log")}</string>
  <key>StandardErrorPath</key>
  <string>{_xml(str(workdir) + "/hd_dailydeals.stderr.log")}</string>

  <!-- launchd starts a slot it missed as soon as the machine wakes, so this
       can land hours late and beside the routine scan. The poll handles
       that itself: far from 3:00 it takes one read and stops, and the sweep
       holds a lock so two processes never price the same set. RunAtLoad
       would only add a run at every login. -->
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
"""


def render_dashboard_plist(label: str, workdir: Path, hd_path: Path) -> str:
    """launchd job for the resident dashboard.

    The only job here that stays running. `hd serve` blocks and owns its event
    loop, so without this the dashboard exists only for as long as somebody
    keeps a terminal window open — which is the whole reason a non-technical
    install needs a terminal after day one.

    KeepAlive restarts it after a crash, a logout or a reboot. ThrottleInterval
    holds a broken install (a missing dashboard extra, an occupied port) to one
    restart every 30s instead of a spin.

    Host and port are deliberately not baked in: `hd serve` reads them from
    settings, so changing the port in .env takes effect on the next restart
    rather than needing the job rewritten.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_xml(label)}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>export PYTHONUNBUFFERED=1; cd {_xml(_shell(workdir))} &amp;&amp; {_xml(_shell(hd_path))} serve</string>
  </array>

  <key>WorkingDirectory</key>
  <string>{_xml(workdir)}</string>

  <key>StandardOutPath</key>
  <string>{_xml(str(workdir) + "/hd_dashboard.stdout.log")}</string>
  <key>StandardErrorPath</key>
  <string>{_xml(str(workdir) + "/hd_dashboard.stderr.log")}</string>

  <!-- Resident: start at login and come back from any exit. -->
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
</dict>
</plist>
"""


def render_crontab(
    workdir: Path, hd_path: Path, slots: list[ScheduleSlot], prune: ScheduleSlot
) -> str:
    """The equivalent crontab for Linux, where launchd does not exist."""
    work, hd = _shell(workdir), _shell(hd_path)
    lines = [
        "# Home Depot clearance monitor",
        "# Times are local. One slot tracks Home Depot's 3:00 ET Daily Deals refresh.",
        '# MAILTO="" so six runs a day do not mail you.',
        'MAILTO=""',
    ]
    for slot in slots:
        lines.append(f"{slot.minute} {slot.hour} * * * cd {work} && {hd} run-once && {hd} notify")
    lines.append("# Retention — nothing else deletes old snapshots.")
    lines.append(f"{prune.minute} {prune.hour} * * * cd {work} && {hd} prune")
    return "\n".join(lines) + "\n"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def is_macos() -> bool:
    return sys.platform == "darwin"


def write_agent(path: Path, contents: str) -> Path:
    """Write a launchd plist, creating LaunchAgents if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return path


async def load_agent(path: Path) -> tuple[bool, str]:
    """Register a launchd job, replacing any previous copy.

    Unloads first so re-running setup updates the schedule instead of failing
    with "service already loaded".
    """
    import asyncio

    async def _run(*args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace").strip()

    await _run("launchctl", "unload", str(path))
    code, output = await _run("launchctl", "load", str(path))
    if code != 0:
        return False, output or f"launchctl exited {code}"
    return True, output
