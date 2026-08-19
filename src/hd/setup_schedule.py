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

import os
import shlex
import shutil
import sys
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

# The routine sweep. Deliberately not on the hour boundary of the deals run.
SCAN_HOURS_ET = (0, 8, 12, 16, 20)
PRUNE_HOUR_ET = 4

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


def scan_slots(*, tz=None, on: datetime | None = None) -> list[ScheduleSlot]:
    """Every local time the scan should run, deals slot included, in order."""
    slots = [_to_local(h, 0, tz=tz, on=on) for h in SCAN_HOURS_ET]
    slots.append(daily_deals_slot(tz=tz, on=on))
    unique = {(s.hour, s.minute) for s in slots}
    return [ScheduleSlot(h, m) for h, m in sorted(unique)]


def prune_slot(*, tz=None, on: datetime | None = None) -> ScheduleSlot:
    return _to_local(PRUNE_HOUR_ET, 30, tz=tz, on=on)


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
