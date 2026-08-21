"""Throttle cooldown that outlives a single run.

Home Depot's 206 quota signal applies to the caller, not to the process, but
the scanner forgets it at exit: the next scheduled run opens a fresh client and
walks straight back into the wall. The 20:00 run on 2026-08-19 was throttled on
its second request for exactly this reason.

A cooldown file carries the signal across runs, so being told "enough" is
honoured until it expires instead of until the process ends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from hd.logging import get_logger

log = get_logger("http.cooldown")


class ThrottleCooldown:
    """A timestamp on disk marking when requests may resume."""

    def __init__(self, path: str | Path, default_seconds: float = 3600.0) -> None:
        self._path = Path(path)
        self._default_seconds = default_seconds

    def active_until(self) -> datetime | None:
        """When the cooldown expires, or None if there is none in force.

        An unreadable or corrupt file yields None. Failing open is deliberate:
        a damaged cooldown file must not silently disable the scanner forever,
        and the live 206 handling still stops a run the moment it is throttled.
        """
        try:
            raw = self._path.read_text().strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not raw:
            return None
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            log.warning("Ignoring unparseable cooldown file", path=str(self._path))
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when if when > datetime.now(timezone.utc) else None

    def remaining_seconds(self) -> float:
        until = self.active_until()
        if until is None:
            return 0.0
        return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())

    def is_active(self) -> bool:
        return self.active_until() is not None

    def start(self, seconds: float | None = None) -> datetime:
        """Begin a cooldown, extending any already in force rather than shortening it."""
        duration = self._default_seconds if seconds is None else seconds
        until = datetime.now(timezone.utc) + timedelta(seconds=duration)
        existing = self.active_until()
        if existing is not None and existing > until:
            return existing
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(until.isoformat())
        except OSError as e:
            # Worth knowing about, but not worth failing the run over: the
            # in-process throttle flag still stops this run.
            log.warning("Could not persist cooldown", error=str(e))
        return until

    def clear(self) -> None:
        try:
            self._path.unlink()
        except (OSError, FileNotFoundError):
            pass
