"""The scanner's own condition, shown where someone will actually see it.

`hd doctor` already detects nearly every way an install dies quietly — a
schedule that stopped, a prune job that was never registered, a degraded API,
a `hd` path that resolves to the wrong interpreter. All of it only speaks when
somebody types a command, which for an install whose owner never opens a
terminal is the same as not detecting it at all.

This puts the same checks on the page, and offers the same repairs as a button.
"""

from __future__ import annotations

import time

from nicegui import ui

from hd.config import Settings
from hd.doctor import FAIL, WARN, Check, apply_fixes, run_checks
from hd.logging import get_logger

log = get_logger("dashboard.health")

# The checks shell out to launchctl and query the database. Cheap, but not
# cheap enough to repeat on every page view of a dashboard that is always up.
_TTL_SECONDS = 300
_cache: tuple[float, list[Check]] | None = None


async def _checks(settings: Settings, *, force: bool = False) -> list[Check]:
    global _cache
    if not force and _cache is not None and time.monotonic() - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    try:
        checks = await run_checks(settings)
    except Exception as exc:
        # A banner that crashes the page it is warning you about is worse than
        # no banner.
        log.warning("Health checks failed", error=str(exc))
        return []
    _cache = (time.monotonic(), checks)
    return checks


def banner_level(checks: list[Check]) -> str:
    """How loudly to say it: "fail", "advisory", or "quiet".

    Split out from the rendering so the rule itself is testable, because the
    rule is the whole point: a warning must never produce a banner. Retiring a
    store you kept the history for warns forever, and a bar that is always lit
    stops being read.
    """
    if any(c.status == FAIL for c in checks):
        return "fail"
    if any(c.status == WARN for c in checks):
        return "advisory"
    return "quiet"


async def render_health_banner(settings: Settings) -> None:
    """Say what is wrong, at the volume the problem deserves.

    A banner means one thing: the scanner is not collecting. Warnings are real
    information but they are not alarms — several are permanent by choice, like
    a store you stopped watching whose price history you kept. Rendering those
    as a banner would leave it lit forever, and a signal that is always on is
    one nobody reads on the day it matters.

    So: FAIL gets the banner. WARN gets a quiet line that opens if you want it.
    """

    @ui.refreshable
    async def banner() -> None:
        checks = await _checks(settings)
        level = banner_level(checks)
        if level == "quiet":
            return
        failed = [c for c in checks if c.status == FAIL]
        warned = [c for c in checks if c.status == WARN]

        async def _fix() -> None:
            actions = await apply_fixes(settings, checks)
            if actions:
                for line in actions:
                    ui.notification(line, type="positive", timeout=8)
            else:
                ui.notification(
                    "Nothing here can be repaired automatically — the detail says "
                    "what to do.",
                    type="warning",
                    timeout=8,
                )
            await _checks(settings, force=True)
            banner.refresh()

        def _lines(checks_: list[Check], cls: str) -> None:
            for c in checks_:
                detail = c.detail if not c.fix else f"{c.detail} — {c.fix}"
                ui.label(detail).classes(cls)

        if level == "fail":
            with ui.element("div").classes("hd-banner fail"):
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.html('<span class="hd-banner-mark"></span>')
                    ui.label("The scanner is not collecting properly").classes("hd-banner-head")
                    ui.element("div").classes("grow")
                    ui.button("Fix it", on_click=_fix).props("flat dense no-caps")
                _lines(failed, "hd-banner-line")
                if warned:
                    _lines(warned, "hd-banner-line muted")
            return

        # Warnings only: present, findable, and not shouting.
        n = len(warned)
        label = f"{n} advisory" if n == 1 else f"{n} advisories"
        with ui.element("div").classes("hd-advisory"):
            with ui.expansion(label).props("dense dense-toggle"):
                _lines(warned, "hd-banner-line")
                ui.button("Fix what can be fixed", on_click=_fix).props("flat dense no-caps")

    await banner()
