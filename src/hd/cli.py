"""CLI entry point using Typer."""

from __future__ import annotations

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from hd.config import Settings
from hd.db.models import AlertType
from hd.logging import setup_logging

app = typer.Typer(name="hd", help="Home Depot Clearance Monitor")
console = Console()


def _run(coro):
    """Run an async coroutine from sync CLI context."""
    return asyncio.run(coro)


@app.command()
def setup() -> None:
    """Interactive first-run setup: find your stores and brands, write .env."""
    from pathlib import Path

    from hd.setup_wizard import run_setup

    raise typer.Exit(_run(run_setup(Path.cwd())))


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
    city: Optional[str] = typer.Option(None, help="Store city (defaults to name)"),
) -> None:
    """Add a store, or update the details of one that already exists."""
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
                # Update in place: store location details are filled in after
                # the row is first seeded, and refusing here left them empty.
                updates = {"name": name, "state": state, "zip": zip_code, "city": city}
                applied = [k for k, v in updates.items() if v is not None]
                for key in applied:
                    setattr(existing, key, updates[key])
                await close_db()
                return "updated" if applied else "exists"

            session.add(Store(
                store_id=store_id,
                name=name,
                state=state,
                zip=zip_code,
                city=city,
            ))

        await close_db()
        return "added"

    result = _run(_add())
    if result == "added":
        console.print(f"[green]Store {store_id} added.[/green]")
    elif result == "updated":
        console.print(f"[green]Store {store_id} updated.[/green]")
    else:
        console.print(f"[yellow]Store {store_id} already exists — pass fields to update.[/yellow]")


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
def browse(
    stores: Optional[str] = typer.Option(None, help="Comma-separated store IDs (default: config)"),
    tier: str = typer.Option("both", help="Tier to run: shelf, network, or both"),
) -> None:
    """Facet-driven brand browse: discover + snapshot every brand item by category."""
    setup_logging()
    settings = Settings()

    async def _browse():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.browse import run_browse

        await _init_tables(settings)
        store_ids = [s.strip() for s in stores.split(",")] if stores else settings.store_list
        tiers = ("shelf", "network") if tier == "both" else (tier,)
        summary = await run_browse(settings=settings, store_ids=store_ids, tiers=tiers)
        await close_db()
        return summary

    summary = _run(_browse())
    msg = (
        f"[green]Browse complete: {summary.products} products, "
        f"{summary.snapshots} snapshots across {summary.walks} walk(s)."
    )
    if summary.truncated_walks:
        msg += f" [yellow]Truncated: {', '.join(summary.truncated_walks)}[/yellow]"
    if summary.aborted:
        msg += " [red](aborted early — throttled)[/red]"
    msg += "[/green]"
    console.print(msg)


@app.command()
def daily_deals(
    force: bool = typer.Option(False, "--force", help="Re-process even if today's set was already swept"),
) -> None:
    """Price today's Daily Deals set (Special Buy of the Day) for configured brands."""
    setup_logging()
    settings = Settings()

    async def _daily():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.daily_deals import run_daily_deals

        await _init_tables(settings)
        summary = await run_daily_deals(settings, force=force)
        await close_db()
        return summary

    s = _run(_daily())
    if s.skipped:
        console.print("[yellow]Skipped — set already processed or page unavailable "
                      "(use --force to re-run).[/yellow]")
    else:
        console.print(
            f"[green]Daily deals ({s.end_date}): {s.items_checked} items checked, "
            f"{s.brand_matches} brand match(es), {s.snapshots} snapshot(s).[/green]"
        )


@app.command()
def run_once(
    mode: str = typer.Option("auto", help="Run mode: auto, full, snapshot-only"),
) -> None:
    """Run pipeline: snapshot -> diff -> alerts (+ discovery on full runs).

    Modes:
      auto          - snapshot-only by default; full discovery once daily at 00:00 UTC
      full          - discovery + snapshots (catalog refresh)
      snapshot-only - snapshots only (clearance/price monitoring)
    """
    setup_logging()
    settings = Settings()

    async def _run_once():
        from pathlib import Path
        from datetime import datetime, timezone
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.discovery import run_discovery
        from hd.pipeline.snapshot import run_snapshots
        from hd.pipeline.diff import run_diff
        from hd.pipeline.alerts import write_alerts
        from hd.http.client import HDClient
        from hd.logging import get_logger

        log = get_logger("pipeline")

        # Determine effective mode
        effective_mode = mode
        if mode == "auto":
            hour = datetime.now(timezone.utc).hour
            effective_mode = "full" if hour == 0 else "snapshot-only"

        log.info("Pipeline starting", mode=effective_mode, browse=settings.browse_enabled)
        await _init_tables(settings)

        if settings.browse_enabled:
            shared_client = HDClient(settings, request_budget=settings.browse_request_budget)
        else:
            shared_client = HDClient(settings)
        product_count = 0
        snapshot_count = 0

        try:
            if settings.browse_enabled:
                # Facet-driven browse replaces keyword discovery + snapshots:
                # both tiers upsert products and append snapshots per page.
                from hd.pipeline.browse import run_browse

                summary = await run_browse(
                    settings=settings,
                    store_ids=settings.store_list,
                    client=shared_client,
                )
                product_count = summary.products
                snapshot_count = summary.snapshots
                effective_mode = "browse"

                # Daily Deals sweep: fires only when the day's set is new, so
                # the 3:10 ET run prices it minutes after launch. Own client —
                # its request budget must not compete with the browse tiers.
                if settings.daily_deals_enabled:
                    from hd.pipeline.daily_deals import run_daily_deals

                    dd = await run_daily_deals(settings)
                    if not dd.skipped:
                        product_count += dd.products
                        snapshot_count += dd.snapshots
                        log.info(
                            "Daily deals swept",
                            end_date=dd.end_date,
                            brand_matches=dd.brand_matches,
                            snapshots=dd.snapshots,
                        )
            else:
                if effective_mode == "full":
                    product_count = await run_discovery(
                        settings=settings,
                        brands=settings.brand_list,
                        max_pages=settings.max_pages,
                        client=shared_client,
                    )
                    log.info("Discovery complete", products=product_count, requests=shared_client.request_count)

                    if settings.stage_delay_seconds > 0:
                        await asyncio.sleep(settings.stage_delay_seconds)

                snapshot_count = await run_snapshots(
                    settings=settings,
                    store_ids=settings.store_list,
                    client=shared_client,
                )
            log.info("Scan complete", products=product_count, rows=snapshot_count, requests=shared_client.request_count)
        finally:
            await shared_client.close()

        # Sanity check: zero snapshots on a snapshot run → likely API error
        if snapshot_count == 0:
            from hd.pipeline.health import emit_health_degraded_alert
            await emit_health_degraded_alert(
                settings,
                ["Zero snapshots"],
                message="Zero snapshots — likely API throttling or error responses",
            )

        alerts_list = await run_diff(settings=settings)
        alert_count = 0
        if alerts_list:
            alert_count = await write_alerts(settings=settings, alerts=alerts_list)
        log.info("Diff complete", alerts=alert_count)

        # Auto-notify Slack when new alerts are found
        sent_count = 0
        if alert_count > 0 and settings.slack_bot_token:
            from hd.dashboard.queries import get_alerts
            from hd.grouping import group_alerts, parse_ts
            from hd.notifiers.formatter import format_slack_blocks
            from hd.notifiers.webhook import post_to_openclaw

            cursor_path = Path(settings.notify_cursor_path)
            cursor_ts = None
            if cursor_path.exists():
                try:
                    cursor_ts = datetime.fromisoformat(cursor_path.read_text().strip())
                except (ValueError, OSError):
                    pass

            hours_back = max(4, 168) if cursor_ts else 4
            recent = await get_alerts(settings, since_hours=hours_back, limit=500)
            if cursor_ts is not None:
                recent = [a for a in recent if parse_ts(a.get("ts")) > cursor_ts]
            recent = [a for a in recent if a.get("alert_type") != "HEALTH_DEGRADED"]

            if recent:
                groups = group_alerts(recent)
                blocks, fallback_text = format_slack_blocks(groups)
                success = await post_to_openclaw(settings, fallback_text, blocks=blocks)
                if success:
                    max_ts = max(
                        (parse_ts(a.get("ts")) for a in recent), default=None
                    )
                    if max_ts is not None:
                        cursor_path.write_text(max_ts.isoformat())
                    sent_count = len(groups)
                    log.info("Slack notification sent", groups=sent_count)
                else:
                    log.warning("Slack notification failed")

        # Update Slack canvas with current deal rundown
        canvas_deals = 0
        if settings.slack_bot_token and snapshot_count > 0:
            try:
                from hd.notifiers.canvas import run_canvas_update
                _, canvas_deals = await run_canvas_update(settings)
                log.info("Canvas updated", deals=canvas_deals)
            except Exception as e:
                log.warning("Canvas update failed", error=str(e))

        await close_db()
        return product_count, snapshot_count, alert_count, sent_count, canvas_deals, effective_mode

    products, snapshots, alerts_count, sent, canvas_deals, effective_mode = _run(_run_once())
    msg = f"[green]Pipeline complete ({effective_mode}): "
    if products:
        msg += f"{products} products, "
    msg += f"{snapshots} snapshots, {alerts_count} alerts."
    if sent:
        msg += f" Sent {sent} group(s) to Slack."
    if canvas_deals:
        msg += f" Canvas: {canvas_deals} deal(s)."
    msg += "[/green]"
    console.print(msg)


@app.command()
def catch_up(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be created without writing"),
) -> None:
    """One-time scan: alert on anything currently ≥50% off or in Special Buys that has never been alerted."""
    setup_logging()
    settings = Settings()

    async def _catch_up():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.diff import run_catch_up
        from hd.pipeline.alerts import write_alerts

        await _init_tables(settings)
        alerts_list = await run_catch_up(settings=settings)

        if dry_run:
            await close_db()
            return alerts_list, 0

        written = 0
        if alerts_list:
            written = await write_alerts(settings=settings, alerts=alerts_list)
        await close_db()
        return alerts_list, written

    alerts_list, written = _run(_catch_up())

    if not alerts_list:
        console.print("[yellow]No catch-up alerts needed — everything is already covered.[/yellow]")
        return

    if dry_run:
        table = Table(title="Catch-up Alerts (dry run)")
        table.add_column("Store", style="green")
        table.add_column("Item", style="white")
        table.add_column("Type", style="magenta")
        table.add_column("Severity", style="red")
        table.add_column("Details", style="dim")

        for a in alerts_list:
            payload = a.payload or {}
            after = payload.get("after", {})
            if a.alert_type == AlertType.DEEP_DISCOUNT:
                details = f"{payload.get('percentage_off', '?')}% off @ ${after.get('price_value', '?')}"
            elif a.alert_type == AlertType.SPECIAL_BUY:
                details = f"Special Buy @ ${after.get('price_value', '?')}"
            elif a.alert_type == AlertType.IN_STORE_CLEARANCE:
                cl = payload.get("clearance_value", "?")
                pct = payload.get("clearance_percentage_off", "?")
                details = f"In-store ${cl} ({pct}% off)"
            else:
                details = payload.get("product_title", "")[:40]
            table.add_row(a.store_id, a.item_id, a.alert_type.value, a.severity.value, details)

        console.print(table)
        console.print(f"\n[cyan]{len(alerts_list)} alert(s) would be created. Run without --dry-run to persist.[/cyan]")
    else:
        console.print(f"[green]Catch-up complete: {written} alerts created.[/green]")


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
        elif row.alert_type.value == "IN_STORE_CLEARANCE":
            cl = payload.get("clearance_value", "?")
            pct = payload.get("clearance_percentage_off", "?")
            details = f"${cl} ({pct}% off) in-store"
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
    force: bool = typer.Option(
        False, "--force", help="Prune even if price stats have not captured every item"
    ),
) -> None:
    """Delete old snapshot rows beyond retention period."""
    setup_logging()
    settings = Settings()

    async def _prune():
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, func, delete
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import ItemPriceStat, StoreSnapshot

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

                # Deleting a snapshot is only safe once its price facts live in
                # item_price_stats; otherwise an item's entire recorded history
                # can vanish with nothing left to say it existed.
                observed = (await session.execute(
                    select(func.count()).select_from(
                        select(StoreSnapshot.store_id, StoreSnapshot.item_id)
                        .where(StoreSnapshot.price_value.isnot(None))
                        .distinct().subquery()
                    )
                )).scalar() or 0
                captured = (await session.execute(
                    select(func.count()).select_from(ItemPriceStat)
                )).scalar() or 0
                uncaptured = max(0, observed - captured)

                if dry_run:
                    return count, 0, uncaptured
                if uncaptured and not force:
                    return count, -1, uncaptured

                if count > 0:
                    await session.execute(
                        delete(StoreSnapshot).where(StoreSnapshot.ts < cutoff)
                    )
                return count, count, uncaptured
        finally:
            await close_db()

    eligible, deleted, uncaptured = _run(_prune())
    if dry_run:
        console.print(f"[yellow]Dry run: {eligible} snapshots eligible for deletion.[/yellow]")
        if uncaptured:
            console.print(
                f"[red]{uncaptured} item(s) have price history not yet captured "
                f"in item_price_stats — run 'hd backfill-stats' first.[/red]"
            )
    elif deleted < 0:
        console.print(
            f"[red]Refusing to prune: {uncaptured} item(s) have price history that "
            f"only exists in store_snapshots.[/red]\n"
            f"Run [bold]hd backfill-stats[/bold] first, or pass --force to delete anyway."
        )
        raise typer.Exit(code=1)
    else:
        console.print(f"[green]Pruned {deleted} old snapshots.[/green]")


@app.command("backfill-stats")
def backfill_stats(
    chunk: int = typer.Option(50_000, help="Rows to read per batch"),
) -> None:
    """Rebuild item_price_stats from the raw snapshots that still exist."""
    setup_logging()
    settings = Settings()

    async def _backfill():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.db.price_stats import backfill

        await _init_tables(settings)
        try:
            return await backfill(settings, chunk_size=chunk)
        finally:
            await close_db()

    scanned, items = _run(_backfill())
    console.print(
        f"[green]Captured {items:,} item(s) from {scanned:,} priced snapshots.[/green]"
    )


@app.command()
def notify(
    since: int = typer.Option(4, help="Fallback hours if no cursor exists"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without sending"),
    reset: bool = typer.Option(False, "--reset", help="Clear cursor and re-send"),
) -> None:
    """Send recent alerts to Slack via OpenClaw webhook."""
    from pathlib import Path

    setup_logging()
    settings = Settings()

    cursor_path = Path(settings.notify_cursor_path)

    # Handle --reset
    if reset and cursor_path.exists():
        cursor_path.unlink()
        console.print("[yellow]Cursor reset.[/yellow]")

    # Read cursor timestamp
    cursor_ts = None
    if cursor_path.exists():
        try:
            from datetime import datetime

            raw = cursor_path.read_text().strip()
            cursor_ts = datetime.fromisoformat(raw)
        except (ValueError, OSError):
            console.print("[yellow]Invalid cursor file, using --since fallback.[/yellow]")

    async def _notify():
        from datetime import datetime, timedelta, timezone

        from hd.db.base import init_db as _init_tables, close_db
        from hd.dashboard.queries import get_alerts
        from hd.grouping import group_alerts
        from hd.notifiers.formatter import format_slack_message, format_slack_blocks
        from hd.notifiers.webhook import post_to_openclaw

        await _init_tables(settings)

        # Determine how far back to query
        if cursor_ts is not None:
            # Query with a generous window; we filter post-query
            hours_back = max(since, 168)  # up to 7 days
        else:
            hours_back = since

        alerts_list = await get_alerts(settings, since_hours=hours_back, limit=500)

        # Filter to alerts after cursor_ts
        from hd.grouping import parse_ts

        if cursor_ts is not None:
            alerts_list = [
                a for a in alerts_list
                if parse_ts(a.get("ts")) > cursor_ts
            ]

        # Filter out HEALTH_DEGRADED (internal, not useful in Slack)
        alerts_list = [
            a for a in alerts_list
            if a.get("alert_type") != "HEALTH_DEGRADED"
        ]

        if not alerts_list:
            await close_db()
            return 0, None, None

        groups = group_alerts(alerts_list)
        blocks, fallback_text = format_slack_blocks(groups)
        plain_message = format_slack_message(groups)

        # Find max timestamp for cursor update
        max_ts = max(
            (parse_ts(a.get("ts")) for a in alerts_list),
            default=None,
        )

        if dry_run:
            console.print(plain_message)
            await close_db()
            return len(groups), max_ts, True

        # Validate Slack token is configured
        if not settings.slack_bot_token:
            console.print("[red]SLACK_BOT_TOKEN not set. Use --dry-run to preview.[/red]")
            await close_db()
            return len(groups), max_ts, False

        success = await post_to_openclaw(settings, fallback_text, blocks=blocks)
        await close_db()
        return len(groups), max_ts, success

    group_count, max_ts, success = _run(_notify())

    if group_count == 0:
        console.print("[yellow]No new alerts to send.[/yellow]")
        return

    if dry_run:
        console.print(f"\n[cyan]--- Dry run: {group_count} alert group(s) above ---[/cyan]")
        return

    if success:
        # Update cursor
        if max_ts is not None:
            cursor_path.write_text(max_ts.isoformat())
        console.print(f"[green]Sent {group_count} alert group(s) to Slack.[/green]")
    else:
        console.print("[red]Webhook delivery failed. Cursor not updated.[/red]")


@app.command()
def canvas_update(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print markdown without sending to Slack"),
    reset: bool = typer.Option(False, "--reset", help="Delete canvas ID file and create a new canvas"),
) -> None:
    """Update the Slack canvas with current deal rundown per store."""
    from pathlib import Path

    setup_logging()
    settings = Settings()

    if reset:
        canvas_path = Path(settings.canvas_id_path)
        if canvas_path.exists():
            canvas_path.unlink()
            console.print("[yellow]Canvas ID reset — will create a new canvas.[/yellow]")

    async def _canvas_update():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.notifiers.canvas import run_canvas_update

        await _init_tables(settings)
        markdown, deal_count = await run_canvas_update(settings, dry_run=dry_run)
        await close_db()
        return markdown, deal_count

    markdown, deal_count = _run(_canvas_update())

    if dry_run:
        console.print(markdown)
        console.print(f"\n[cyan]--- Dry run: {deal_count} deal(s) across all stores ---[/cyan]")
    else:
        console.print(f"[green]Canvas updated with {deal_count} deal(s).[/green]")


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
