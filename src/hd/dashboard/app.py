"""NiceGUI app factory, lifecycle hooks, and ui.run()."""

from __future__ import annotations

from nicegui import app, ui

from hd.config import Settings


def run_dashboard(settings: Settings) -> None:
    """Start the NiceGUI dashboard. This is blocking — owns the event loop."""
    import hd.dashboard._state as _state

    _state.settings = settings

    @app.on_startup
    async def on_startup() -> None:
        from hd.db.base import init_db

        await init_db(settings)

    @app.on_shutdown
    async def on_shutdown() -> None:
        from hd.db.base import close_db

        await close_db()

    # Import pages to register @ui.page routes
    from hd.dashboard.pages import alerts, overview, products, stores  # noqa: F401

    ui.run(
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        title=settings.dashboard_title,
        dark=settings.dashboard_dark_mode,
        reload=False,
        show=False,
    )
