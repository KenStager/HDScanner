"""Shared navigation header for all dashboard pages."""

from __future__ import annotations

from nicegui import ui

_NAV_LINKS = [
    ("Overview", "/"),
    ("Products", "/products"),
    ("Alerts", "/alerts"),
    ("Stores", "/stores"),
]


def render_header(title: str = "HD Clearance Monitor", current_path: str = "/") -> None:
    """Render a fixed header with navigation links.

    The link matching *current_path* gets a bold + underline style so
    users can tell which page they are on.
    """
    with ui.header().classes("items-center justify-between"):
        ui.label(title).classes("text-h6 font-bold")
        with ui.row().classes("gap-4"):
            for label, href in _NAV_LINKS:
                if href == current_path:
                    ui.link(label, href).classes(
                        "text-white font-bold underline underline-offset-4"
                    )
                else:
                    ui.link(label, href).classes(
                        "text-white no-underline hover:underline opacity-80"
                    )
