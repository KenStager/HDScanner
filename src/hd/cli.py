"""CLI entry point using Typer."""

from __future__ import annotations

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from hd.config import Settings
from hd.logging import setup_logging

app = typer.Typer(name="hd", help="Home Depot Clearance Monitor")
console = Console()


def _run(coro):
    """Run an async coroutine from sync CLI context."""
    return asyncio.run(coro)


@app.command()
def init_db() -> None:
    """Create/migrate tables and seed default stores."""
    setup_logging()
    settings = Settings()

    async def _init():
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import Store

        await _init_tables(settings)

        async with get_session(settings) as session:
            from sqlalchemy import select

            for store_id in settings.store_list:
                result = await session.execute(
                    select(Store).where(Store.store_id == store_id)
                )
                if result.scalar_one_or_none() is None:
                    session.add(Store(store_id=store_id))

        await close_db()

    _run(_init())
    console.print(f"[green]Database initialized. Stores seeded: {settings.store_list}[/green]")


@app.command()
def add_store(
    store_id: str = typer.Argument(..., help="Store ID to add"),
    name: Optional[str] = typer.Option(None, help="Store name"),
    state: Optional[str] = typer.Option(None, help="Store state"),
    zip_code: Optional[str] = typer.Option(None, "--zip", help="Store ZIP code"),
) -> None:
    """Add a store to the database."""
    setup_logging()
    settings = Settings()

    async def _add():
        from hd.db.base import get_session, close_db, init_db as _init_tables
        from hd.db.models import Store
        from sqlalchemy import select

        await _init_tables(settings)

        async with get_session(settings) as session:
            result = await session.execute(
                select(Store).where(Store.store_id == store_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                console.print(f"[yellow]Store {store_id} already exists.[/yellow]")
                await close_db()
                return False

            session.add(Store(
                store_id=store_id,
                name=name,
                state=state,
                zip=zip_code,
            ))

        await close_db()
        return True

    added = _run(_add())
    if added:
        console.print(f"[green]Store {store_id} added.[/green]")


@app.command()
def discover(
    brand: Optional[list[str]] = typer.Option(None, help="Brand(s) to discover"),
    pages: int = typer.Option(0, help="Max pages per brand (0 = use config)"),
    clearance_only: bool = typer.Option(False, "--clearance-only", help="Only discover clearance items"),
) -> None:
    """Run product discovery pipeline."""
    setup_logging()
    settings = Settings()

    async def _discover():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.discovery import run_discovery

        await _init_tables(settings)
        brands = brand if brand else settings.brand_list
        max_pages = pages if pages > 0 else settings.max_pages
        count = await run_discovery(
            settings=settings,
            brands=brands,
            max_pages=max_pages,
            clearance_only=clearance_only,
        )
        await close_db()
        return count

    count = _run(_discover())
    console.print(f"[green]Discovery complete: {count} products found/updated.[/green]")


@app.command()
def snapshot(
    stores: Optional[str] = typer.Option(None, help="Comma-separated store IDs"),
    limit: int = typer.Option(0, help="Max products to snapshot (0 = all)"),
) -> None:
    """Fetch pricing/inventory snapshots for active products."""
    setup_logging()
    settings = Settings()

    async def _snapshot():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.snapshot import run_snapshots

        await _init_tables(settings)
        store_ids = stores.split(",") if stores else settings.store_list
        count = await run_snapshots(
            settings=settings,
            store_ids=store_ids,
            limit=limit if limit > 0 else None,
        )
        await close_db()
        return count

    count = _run(_snapshot())
    console.print(f"[green]Snapshots complete: {count} rows inserted.[/green]")


@app.command()
def run_once() -> None:
    """Run full pipeline: discover -> snapshot -> diff -> alerts."""
    setup_logging()
    settings = Settings()

    async def _run_once():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.discovery import run_discovery
        from hd.pipeline.snapshot import run_snapshots
        from hd.pipeline.diff import run_diff
        from hd.pipeline.alerts import write_alerts
        from hd.logging import get_logger

        log = get_logger("pipeline")

        await _init_tables(settings)

        product_count = await run_discovery(
            settings=settings,
            brands=settings.brand_list,
            max_pages=settings.max_pages,
        )
        log.info("Discovery complete", products=product_count)

        if settings.stage_delay_seconds > 0:
            log.info("Stage delay", seconds=settings.stage_delay_seconds)
            await asyncio.sleep(settings.stage_delay_seconds)

        snapshot_count = await run_snapshots(
            settings=settings,
            store_ids=settings.store_list,
        )
        log.info("Snapshots complete", rows=snapshot_count)

        # Sanity check: products found but zero snapshots → likely API error responses
        if product_count > 0 and snapshot_count == 0:
            from hd.pipeline.health import emit_health_degraded_alert
            await emit_health_degraded_alert(
                settings,
                ["Zero snapshots despite active products"],
                message=f"Zero snapshots despite {product_count} active products — likely API error responses",
            )

        alerts_list = await run_diff(settings=settings)
        alert_count = 0
        if alerts_list:
            alert_count = await write_alerts(settings=settings, alerts=alerts_list)
        log.info("Diff complete", alerts=alert_count)

        await close_db()
        return product_count, snapshot_count, alert_count

    products, snapshots, alerts_count = _run(_run_once())
    console.print(
        f"[green]Pipeline complete: {products} products, "
        f"{snapshots} snapshots, {alerts_count} alerts.[/green]"
    )


@app.command()
def alerts(
    limit: int = typer.Option(20, help="Number of alerts to show"),
    type_filter: Optional[str] = typer.Option(None, "--type", help="Filter by alert type"),
    since: Optional[int] = typer.Option(None, help="Show alerts from last N hours"),
) -> None:
    """Print recent alerts."""
    setup_logging()
    settings = Settings()

    async def _alerts():
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, desc
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import Alert, AlertType

        await _init_tables(settings)

        async with get_session(settings) as session:
            stmt = select(Alert)

            if type_filter:
                try:
                    at = AlertType(type_filter)
                    stmt = stmt.where(Alert.alert_type == at)
                except ValueError:
                    console.print(f"[red]Unknown alert type: {type_filter}[/red]")
                    return []

            if since:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=since)
                stmt = stmt.where(Alert.ts >= cutoff)

            stmt = stmt.order_by(desc(Alert.ts)).limit(limit)

            result = await session.execute(stmt)
            rows = result.scalars().all()

        await close_db()
        return rows

    rows = _run(_alerts())

    if not rows:
        console.print("[yellow]No alerts found.[/yellow]")
        return

    table = Table(title="Recent Alerts")
    table.add_column("Time", style="cyan")
    table.add_column("Store", style="green")
    table.add_column("Item", style="white")
    table.add_column("Type", style="magenta")
    table.add_column("Severity", style="red")
    table.add_column("Details", style="dim")

    for row in rows:
        payload = row.payload or {}
        details = ""
        if row.alert_type.value == "PRICE_DROP":
            before = payload.get("before", {}).get("price_value", "?")
            after = payload.get("after", {}).get("price_value", "?")
            details = f"${before} -> ${after}"
        elif row.alert_type.value == "CLEARANCE":
            pct = payload.get("after", {}).get("percentage_off", "?")
            details = f"{pct}% off"
        else:
            title = payload.get("product_title", "")
            details = title[:40] if title else ""

        table.add_row(
            str(row.ts)[:19],
            row.store_id,
            row.item_id,
            row.alert_type.value,
            row.severity.value,
            details,
        )

    console.print(table)


@app.command()
def health() -> None:
    """Print last run health status."""
    setup_logging()
    settings = Settings()

    async def _health():
        from sqlalchemy import select, desc, func
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import Alert, AlertType, StoreSnapshot, Product

        await _init_tables(settings)

        async with get_session(settings) as session:
            # Check for recent HEALTH_DEGRADED alerts
            result = await session.execute(
                select(Alert)
                .where(Alert.alert_type == AlertType.HEALTH_DEGRADED)
                .order_by(desc(Alert.ts))
                .limit(1)
            )
            degraded_alert = result.scalar_one_or_none()

            # Get counts
            product_count = (await session.execute(
                select(func.count()).select_from(Product).where(Product.is_active.is_(True))
            )).scalar() or 0

            snapshot_count = (await session.execute(
                select(func.count()).select_from(StoreSnapshot)
            )).scalar() or 0

            latest_snapshot = (await session.execute(
                select(StoreSnapshot.ts).order_by(desc(StoreSnapshot.ts)).limit(1)
            )).scalar_one_or_none()

        await close_db()
        return degraded_alert, product_count, snapshot_count, latest_snapshot

    degraded, products, snapshots, latest_ts = _run(_health())

    status = "[red]DEGRADED[/red]" if degraded else "[green]HEALTHY[/green]"
    console.print(f"Status: {status}")
    console.print(f"Active products: {products}")
    console.print(f"Total snapshots: {snapshots}")
    if latest_ts:
        console.print(f"Latest snapshot: {str(latest_ts)[:19]}")
    if degraded:
        console.print(f"[red]Last degraded alert: {str(degraded.ts)[:19]}[/red]")
        payload = degraded.payload or {}
        if "message" in payload:
            console.print(f"[red]  {payload['message']}[/red]")


@app.command()
def prune(
    days: int = typer.Option(0, help="Retention days (0 = use config)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show count without deleting"),
) -> None:
    """Delete old snapshot rows beyond retention period."""
    setup_logging()
    settings = Settings()

    async def _prune():
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, func, delete
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import StoreSnapshot

        await _init_tables(settings)

        retention_days = days if days > 0 else settings.snapshot_retention_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        try:
            async with get_session(settings) as session:
                count_result = await session.execute(
                    select(func.count()).select_from(StoreSnapshot).where(
                        StoreSnapshot.ts < cutoff
                    )
                )
                count = count_result.scalar() or 0

                if dry_run:
                    return count, 0

                if count > 0:
                    await session.execute(
                        delete(StoreSnapshot).where(StoreSnapshot.ts < cutoff)
                    )
                return count, count
        finally:
            await close_db()

    eligible, deleted = _run(_prune())
    if dry_run:
        console.print(f"[yellow]Dry run: {eligible} snapshots eligible for deletion.[/yellow]")
    else:
        console.print(f"[green]Pruned {deleted} old snapshots.[/green]")


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, help="Bind host (overrides config)"),
    port: Optional[int] = typer.Option(None, help="Bind port (overrides config)"),
    dark: bool = typer.Option(True, help="Dark mode"),
) -> None:
    """Start the NiceGUI web dashboard."""
    setup_logging()
    settings = Settings()

    if host:
        settings.dashboard_host = host
    if port:
        settings.dashboard_port = port
    settings.dashboard_dark_mode = dark

    try:
        from hd.dashboard.app import run_dashboard
    except ImportError:
        console.print("[red]NiceGUI not installed. Run: pip install -e '.[dashboard]'[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Starting dashboard at http://{settings.dashboard_host}:{settings.dashboard_port}[/green]"
    )
    run_dashboard(settings)  # Blocking — owns the event loop


if __name__ == "__main__":
    app()
