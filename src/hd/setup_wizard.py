"""Interactive first-run setup.

The scanner's settings were always configurable; they were never discoverable.
Nobody knows their Home Depot store id, and brand facet tokens are opaque
strings with no public lookup. This asks for the two things a person does know
— a ZIP code and a brand name — and finds the rest through the API.

Everything written here is verified before it is written. A store comes back
from the API with its city and zip attached rather than typed from memory, and
a brand token is walked to confirm it returns products. The alternative is the
failure this module exists to prevent: a config that looks right, runs
cleanly, and scans nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape as _e

from hd.config import Settings
from hd.hd_api.stores import (
    InvalidZipCode,
    StoreLookupError,
    StoreLookupThrottled,
    StoreResult,
    search_stores,
)
from hd.logging import get_logger

log = get_logger("setup_wizard")

DEFAULT_RADIUS_MILES = 25.0
WIDER_RADII = (50.0, 100.0)

# Values needing quotes in a .env: anything with whitespace or a comment
# marker, which python-dotenv would otherwise truncate.
_NEEDS_QUOTING = re.compile(r"[\s#]")
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class SetupAborted(RuntimeError):
    """The user chose to stop, or setup cannot sensibly continue."""


def _cancelled_exceptions() -> tuple[type[BaseException], ...]:
    """Exceptions meaning "the user pressed Ctrl-C".

    click converts Ctrl-C and EOF at a prompt into click.Abort, so catching
    only KeyboardInterrupt/EOFError caught nothing at a prompt and the user got
    click's bare "Aborted!" instead of an explanation of what was written.
    """
    try:
        from click.exceptions import Abort
    except ImportError:  # pragma: no cover - click ships with typer
        return (KeyboardInterrupt, EOFError)
    return (KeyboardInterrupt, EOFError, Abort)


_CANCELLED = _cancelled_exceptions()


def describe_url(url: str) -> str:
    """Human label for a database URL, never showing its password."""
    from hd.setup_database import describe

    return describe(url)


# ── .env editing ──────────────────────────────────────────────────────────────


class EnvFile:
    """A .env file edited in place, preserving comments, order and spacing.

    Rewriting the file from a dict would discard the explanatory comments that
    make the config readable, so existing keys are edited on their own line and
    only genuinely new keys are appended.
    """

    def __init__(self, path: Path, lines: list[str] | None = None) -> None:
        self.path = Path(path)
        self._lines: list[str] = list(lines or [])

    @classmethod
    def load(cls, path: str | Path) -> EnvFile:
        p = Path(path)
        if not p.exists():
            return cls(p, [])
        text = p.read_text(encoding="utf-8", errors="replace")
        return cls(p, text.split("\n"))

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def _indices_of(self, key: str) -> list[int]:
        return [
            i for i, line in enumerate(self._lines)
            if (m := _KEY_LINE.match(line)) and m.group(1) == key
        ]

    def _index_of(self, key: str) -> int | None:
        """Index of the line that actually takes effect.

        python-dotenv keeps the LAST assignment, so a hand-edited file with a
        duplicate key would otherwise have setup edit a line nobody reads —
        the wizard reports success while the scanner runs on the old value.
        """
        found = self._indices_of(key)
        return found[-1] if found else None

    def get(self, key: str) -> str | None:
        i = self._index_of(key)
        if i is None:
            return None
        _, _, raw = self._lines[i].partition("=")
        value = raw.strip()
        if value[:1] in {'"', "'"} and value[:1] == value[-1:] and len(value) > 1:
            return value[1:-1]
        # Strip an inline comment from an unquoted value, as dotenv does.
        return value.split(" #", 1)[0].strip()

    def set(self, key: str, value: str) -> None:
        """Set a key, collapsing any duplicate assignments to one line."""
        found = self._indices_of(key)
        if not found:
            self._lines.append(f"{key}={self._quote(value)}")
            return

        keep = found[-1]
        # Preserve an `export ` prefix — dropping it would silently stop the
        # variable being exported for anyone sourcing this file from a shell.
        prefix = "export " if self._lines[keep].lstrip().startswith("export ") else ""
        self._lines[keep] = f"{prefix}{key}={self._quote(value)}"
        for i in reversed(found[:-1]):
            del self._lines[i]

    def set_many(self, values: dict[str, str]) -> None:
        for k, v in values.items():
            self.set(k, v)

    @staticmethod
    def _quote(value: str) -> str:
        if value and _NEEDS_QUOTING.search(value):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value

    def render(self) -> str:
        body = "\n".join(self._lines)
        return body + "\n" if body and not body.endswith("\n") else body

    def save(self) -> None:
        """Write atomically with owner-only permissions — the file holds tokens.

        Written to a temporary file and renamed, so an interrupt or a full disk
        cannot leave a truncated .env where the user's only copy of their Slack
        token used to be. Created 0600 rather than chmod-ed afterwards, which
        would expose it at the default umask for the width of the write.
        """
        import os

        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.render())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


# ── Selection parsing ─────────────────────────────────────────────────────────


def parse_selection(raw: str, count: int) -> list[int]:
    """Parse "1,3" or "2" or "1-3" into zero-based indices.

    Raises ValueError with a usable message rather than silently dropping the
    parts it did not understand — a mis-parsed selection would configure the
    wrong store.
    """
    text = raw.strip()
    if not text:
        raise ValueError("Nothing selected")

    picked: list[int] = []
    for chunk in re.split(r"[,\s]+", text):
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise ValueError(f"{chunk!r} is not a number or range") from None
            if lo > hi:
                lo, hi = hi, lo
            candidates = range(lo, hi + 1)
        else:
            try:
                candidates = [int(chunk)]
            except ValueError:
                raise ValueError(f"{chunk!r} is not a number") from None

        for n in candidates:
            if not 1 <= n <= count:
                raise ValueError(f"{n} is not in range 1-{count}")
            if n - 1 not in picked:
                picked.append(n - 1)

    if not picked:
        raise ValueError("Nothing selected")
    return picked


# ── Preflight ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    problems: list[str]
    notes: list[str]


def preflight(root: Path, *, want_dashboard: bool = True) -> PreflightResult:
    """Check the things that would make setup fail confusingly later."""
    problems: list[str] = []
    notes: list[str] = []

    import sys

    if sys.version_info < (3, 11):
        problems.append(
            f"Python 3.11+ required, found {sys.version_info.major}.{sys.version_info.minor}"
        )

    if not root.exists():
        problems.append(f"Project directory not found: {root}")
    elif not _is_writable(root):
        problems.append(f"Cannot write to {root} — setup needs to create .env and the database")
    else:
        env_path = root / ".env"
        if env_path.exists() and not _is_writable(env_path):
            problems.append(
                f"{env_path} exists but is not writable — setup could not save your answers"
            )

    if not _have_curl():
        problems.append("curl not found on PATH — the scanner uses it for every request")

    if want_dashboard:
        try:
            import nicegui  # noqa: F401
        except ImportError:
            notes.append(
                'Dashboard not installed. Run: pip install -e ".[dashboard]" to enable `hd serve`'
            )

    return PreflightResult(ok=not problems, problems=problems, notes=notes)


def _is_writable(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK)


def _have_curl() -> bool:
    import shutil

    return shutil.which("curl") is not None


# ── Store discovery ───────────────────────────────────────────────────────────


async def find_stores_for_zip(
    zip_code: str,
    *,
    radius_miles: float = DEFAULT_RADIUS_MILES,
    endpoint: str | None = None,
    widen: bool = True,
) -> tuple[list[StoreResult], float]:
    """Find stores near a ZIP, widening the radius when nothing is in range.

    Returns (stores, radius_actually_used). An empty list means no store was
    found even at the widest radius — distinct from a lookup failure, which
    raises.
    """
    kwargs = {"endpoint": endpoint} if endpoint else {}
    radii = [radius_miles] + ([r for r in WIDER_RADII if r > radius_miles] if widen else [])

    for radius in radii:
        stores = await search_stores(zip_code, radius_miles=radius, limit=15, **kwargs)
        if stores:
            return stores, radius
        log.info("No stores in range, widening", zip=zip_code, radius=radius)

    return [], radii[-1]


def store_env_value(stores: list[StoreResult]) -> str:
    """The STORES= value for the chosen stores."""
    return ",".join(s.store_id for s in stores)


# ── Interactive flow ──────────────────────────────────────────────────────────


async def _prompt_stores(console, settings: Settings) -> list[StoreResult]:
    """Ask for a ZIP, show what is nearby, and let the user pick."""
    import typer
    from rich.table import Table

    console.print("\n[bold]Which stores should be watched?[/bold]")
    console.print("[dim]Clearance is per-store, so pick the ones you can actually drive to.[/dim]")

    while True:
        zip_code = typer.prompt("  ZIP code").strip()
        try:
            stores, radius = await find_stores_for_zip(zip_code)
        except InvalidZipCode as exc:
            console.print(f"  [yellow]{_e(str(exc))}[/yellow]")
            continue
        except StoreLookupThrottled as exc:
            console.print(f"  [yellow]{_e(str(exc))}[/yellow]")
            if not typer.confirm("  Try again?", default=True):
                raise SetupAborted("Rate limited during store lookup") from exc
            continue
        except StoreLookupError as exc:
            console.print(f"  [red]{_e(str(exc))}[/red]")
            if not typer.confirm("  Try a different ZIP?", default=True):
                raise SetupAborted("Store lookup failed") from exc
            continue

        if not stores:
            console.print(
                f"  [yellow]No Home Depot within {radius:.0f} miles of "
                f"{_e(zip_code)}.[/yellow]"
            )
            continue

        table = Table(show_header=True, header_style="bold")
        table.add_column("#", justify="right", width=3)
        table.add_column("Store")
        table.add_column("Location")
        table.add_column("Distance", justify="right")
        for i, s in enumerate(stores, 1):
            where = ", ".join(p for p in (s.city, s.state) if p)
            dist = f"{s.distance_miles:.1f} mi" if s.distance_miles is not None else "-"
            table.add_row(
                str(i),
                f"{_e(s.name or '(unnamed)')} [dim]({_e(s.store_id)})[/dim]",
                _e(where),
                dist,
            )
        console.print(table)

        raw = typer.prompt("  Select store(s), e.g. 1 or 1,3", default="1")
        try:
            picked = parse_selection(raw, len(stores))
        except ValueError as exc:
            console.print(f"  [yellow]{_e(str(exc))}[/yellow]")
            continue

        chosen = [stores[i] for i in picked]
        for s in chosen:
            if not s.is_complete:
                console.print(
                    f"  [yellow]Note: store {_e(s.store_id)} is missing address details, "
                    "so its store-page links will be omitted.[/yellow]"
                )
        console.print("  [green]Selected:[/green] " + _e(", ".join(s.label for s in chosen)))
        return chosen


async def _prompt_brands(console, settings: Settings, store_id: str) -> list:
    """Ask for brand names and resolve each to a verified facet token."""
    import typer

    from hd.http.client import HDClient
    from hd.pipeline.brands import (
        BrandResolutionError,
        BrandThrottled,
        resolve_brand,
        suggest_brands,
        list_brands,
    )

    console.print("\n[bold]Which brands should be tracked?[/bold]")
    console.print(
        "[dim]Each brand is matched to Home Depot's own catalog token, so a name that "
        "does not exist is caught now rather than scanning nothing later.[/dim]"
    )

    client = HDClient(settings, request_budget=60)
    matches: list = []
    available: dict = {}

    try:
        return await _resolve_brands(console, settings, store_id, client, matches, available)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


async def _resolve_brands(console, settings, store_id, client, matches, available) -> list:
    """The brand prompt loop, split out so the client is always released."""
    import typer

    from hd.pipeline.brands import (
        BrandResolutionError,
        BrandThrottled,
        resolve_brand,
        suggest_brands,
        list_brands,
    )

    while True:
        name = typer.prompt("  Brand name (e.g. Milwaukee, DEWALT, Ryobi)").strip()
        if not name:
            continue
        try:
            match = await resolve_brand(client, settings, name, store_id)
        except BrandThrottled as exc:
            console.print(f"  [yellow]{_e(str(exc))}[/yellow]")
            continue
        except BrandResolutionError as exc:
            console.print(f"  [red]{_e(str(exc))}[/red]")
            raise SetupAborted("Could not read Home Depot's brand list") from exc

        if match is None:
            if not available:
                try:
                    available = await list_brands(client, settings, store_id)
                except BrandResolutionError:
                    available = {}
            hints = suggest_brands(name, available) if available else []
            if hints:
                console.print(
                    f"  [yellow]No brand called {_e(repr(name))}. "
                    f"Did you mean: {_e(', '.join(hints))}?[/yellow]"
                )
            else:
                console.print(f"  [yellow]No brand called {_e(repr(name))} in the tools catalog.[/yellow]")
            continue

        if any(m.name == match.name for m in matches):
            console.print(f"  [dim]{_e(match.name)} already added.[/dim]")
        else:
            matches.append(match)
            console.print(
                f"  [green]{match.name}[/green] -> token [cyan]{match.token}[/cyan] "
                f"({match.verified_total:,} products)"
            )

        if not typer.confirm("  Add another brand?", default=False):
            return matches


def _prompt_filters(console) -> str:
    """Optional product-line narrowing, e.g. M12/M18."""
    import typer

    console.print("\n[bold]Narrow to specific product lines? (optional)[/bold]")
    console.print(
        "[dim]A brand can be large. Filters match text in the title or model number, "
        "so M12,M18 keeps only those Milwaukee lines. Leave blank to track everything.[/dim]"
    )
    return typer.prompt("  Filters (comma separated, or blank)", default="", show_default=False).strip()


# ── Persistence ───────────────────────────────────────────────────────────────


async def seed_stores(settings: Settings, stores: list[StoreResult]) -> int:
    """Create the tables and write complete Store rows.

    Writes name, city, state and zip together. `hd init-db` seeds bare ids,
    which is how a store ends up in the database with no address and no
    working store-page link.
    """
    from sqlalchemy import select

    from hd.db.base import close_db, get_session, init_db
    from hd.db.models import Store

    await init_db(settings)
    written = 0
    async with get_session(settings) as session:
        for s in stores:
            existing = (
                await session.execute(select(Store).where(Store.store_id == s.store_id))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Store(store_id=s.store_id, name=s.name, city=s.city, state=s.state, zip=s.zip)
                )
            else:
                existing.name = s.name or existing.name
                existing.city = s.city or existing.city
                existing.state = s.state or existing.state
                existing.zip = s.zip or existing.zip
            written += 1
    await close_db()
    return written


def build_env_values(
    stores: list[StoreResult], brands: list, filters: str
) -> dict[str, str]:
    """The config these choices imply.

    BRANDS and BRAND_TOKENS are produced together and never separately — a
    brand without its token is the silent-empty-scan failure.
    """
    return {
        "STORES": store_env_value(stores),
        "BRANDS": ",".join(b.name for b in brands),
        "BRAND_TOKENS": ",".join(b.config_entry for b in brands),
        "PRODUCT_LINE_FILTERS": filters,
    }


# ── Orchestration ─────────────────────────────────────────────────────────────


async def run_setup(root: Path | None = None) -> int:
    """Walk a new install from nothing to a working configuration.

    Returns a process exit code.
    """
    import typer
    from rich.console import Console

    console = Console()
    root = Path(root or Path.cwd())

    console.print("[bold]Home Depot clearance monitor — setup[/bold]")
    console.print("[dim]Finds your stores and brands through Home Depot's own catalog.[/dim]")

    checks = preflight(root)
    for note in checks.notes:
        console.print(f"  [yellow]note:[/yellow] {_e(note)}")
    if not checks.ok:
        for problem in checks.problems:
            console.print(f"  [red]blocked:[/red] {_e(problem)}")
        return 1

    env_path = root / ".env"
    env = EnvFile.load(env_path)
    if env.exists and env.get("STORES"):
        console.print(
            f"\n[yellow]{_e(str(env_path))} already configures "
            f"STORES={_e(env.get('STORES') or '')}[/yellow]"
        )
        if not typer.confirm("  Reconfigure?", default=False):
            console.print("  Nothing changed.")
            return 0

    try:
        database_url = await _prompt_database(console, root)
    except SetupAborted as exc:
        console.print(f"\n[red]Setup stopped: {_e(str(exc))}[/red]")
        return 1
    except _CANCELLED:
        console.print("\n[yellow]Setup cancelled. Nothing was written.[/yellow]")
        return 130

    settings = Settings(database_url=database_url)

    try:
        stores = await _prompt_stores(console, settings)
        brands = await _prompt_brands(console, settings, stores[0].store_id)
        if not brands:
            console.print("[red]No brands configured — nothing would be scanned.[/red]")
            return 1
        filters = _prompt_filters(console)
        slack_values = await _prompt_slack(console)
    except SetupAborted as exc:
        console.print(f"\n[red]Setup stopped: {_e(str(exc))}[/red]")
        return 1
    except _CANCELLED:
        # The database exists by now; say so rather than claiming nothing happened.
        console.print(
            "\n[yellow]Setup cancelled.[/yellow] "
            f"[dim]The database at {_e(describe_url(database_url))} was created; "
            "no configuration was written.[/dim]"
        )
        return 130

    values = build_env_values(stores, brands, filters)
    values.update(slack_values)
    values["DATABASE_URL"] = database_url
    env.set_many(values)
    env.save()
    console.print(f"\n[green]Wrote[/green] {_e(str(env_path))}")

    try:
        written = await seed_stores(Settings(database_url=database_url), stores)
    except Exception as exc:  # noqa: BLE001 - reported, not a traceback
        console.print(f"[red]Could not write stores to the database:[/red] {_e(str(exc))}")
        console.print("  [dim]The configuration is saved. Fix the database, then run "
                      "`hd setup` again.[/dim]")
        return 1
    console.print(f"[green]Stores recorded[/green] — {_e(str(written))} store(s)")

    console.print("\n[bold]Configured:[/bold]")
    for s in stores:
        console.print(f"  store  {_e(s.label)}")
    for b in brands:
        console.print(f"  brand  {_e(b.name)} [dim]({_e(b.token)}, {b.verified_total:,} products)[/dim]")
    if filters:
        console.print(f"  filters {_e(filters)}")
    if slack_values.get("SLACK_CHANNEL_ID"):
        console.print(f"  slack  channel {_e(slack_values['SLACK_CHANNEL_ID'])}")
    elif slack_values:
        console.print("  slack  token saved, no channel — alerts off")

    try:
        await _prompt_verify(console, Settings(database_url=database_url), stores[0].store_id)
        await _prompt_schedule(console, root)
    except _CANCELLED:
        console.print("\n[dim]Skipped the remaining optional steps.[/dim]")

    console.print("\n[bold]Next:[/bold] run [cyan]hd run-once[/cyan] for a full scan.")
    return 0


# ── Slack ─────────────────────────────────────────────────────────────────────


async def _prompt_slack(console) -> dict[str, str]:
    """Configure Slack delivery. Returns env values, empty if skipped.

    Skipping is a first-class outcome: the dashboard and `hd alerts` work
    without Slack, so nothing here is allowed to block a working install.
    """
    import typer

    from hd.setup_slack import (
        SCOPE_ALERTS,
        SCOPE_CANVAS,
        SlackSetupError,
        send_test_message,
        verify_token,
    )

    console.print("\n[bold]Send deal alerts to Slack? (optional)[/bold]")
    console.print(
        "[dim]Needs a Slack app with the chat:write scope. Without this the deals are "
        "still visible via `hd alerts` and the dashboard.[/dim]"
    )
    if not typer.confirm("  Set up Slack?", default=False):
        return {}

    console.print(
        "[dim]  Create an app at api.slack.com/apps, add the chat:write bot scope, "
        "install it, then copy the Bot User OAuth Token (xoxb-...).[/dim]"
    )

    identity = None
    token = ""
    while identity is None:
        token = typer.prompt("  Bot token", hide_input=True).strip()
        try:
            identity = await verify_token(token)
        except SlackSetupError as exc:
            console.print(f"  [yellow]{_e(str(exc))}[/yellow]")
            if not typer.confirm("  Try another token?", default=True):
                console.print("  [dim]Skipping Slack.[/dim]")
                return {}

    console.print(f"  [green]Connected[/green] to {_e(identity.team)} as {_e(identity.bot_name)}")

    for scope in identity.missing(SCOPE_ALERTS):
        console.print(
            f"  [yellow]The token is missing {scope}. Add it in OAuth & Permissions, "
            "reinstall the app, then rerun setup.[/yellow]"
        )

    values: dict[str, str] = {"SLACK_BOT_TOKEN": token}

    console.print(
        "\n[dim]  Channel id is on the channel's About tab, e.g. C0123456789. "
        "Invite the bot first with /invite @your-app.[/dim]"
    )
    while True:
        channel = typer.prompt("  Channel id").strip()
        try:
            await send_test_message(
                token, channel, "Home Depot clearance monitor is connected. Deals will land here."
            )
        except SlackSetupError as exc:
            console.print(f"  [yellow]{_e(str(exc))}[/yellow]")
            if typer.confirm("  Try a different channel?", default=True):
                continue
            console.print("  [dim]Saving the token without a channel; alerts stay off.[/dim]")
            return values

        console.print("  [green]Test message delivered.[/green] Check the channel.")
        values["SLACK_CHANNEL_ID"] = channel
        break

    # Canvas is genuinely optional, and unavailable on free workspaces.
    if identity.missing(SCOPE_CANVAS):
        console.print(
            f"  [dim]Deal rundown canvas needs the {SCOPE_CANVAS} scope, which this token "
            "lacks. Alerts will still be sent.[/dim]"
        )
    else:
        console.print(
            "[dim]  A canvas keeps a live rundown of current deals. Free Slack workspaces "
            "cannot create one; alerts are unaffected either way.[/dim]"
        )
        if not typer.confirm("  Enable the deal rundown canvas?", default=True):
            values["CANVAS_ENABLED"] = "false"

    return values


# ── Scheduling ────────────────────────────────────────────────────────────────


async def _prompt_schedule(console, root: Path) -> bool:
    """Offer to install the recurring jobs. Returns True if anything was set up."""
    import typer

    from hd.setup_schedule import (
        hd_executable,
        is_macos,
        label_for,
        launch_agents_dir,
        load_agent,
        prune_slot,
        render_crontab,
        render_prune_plist,
        render_scan_plist,
        scan_slots,
        write_agent,
    )

    console.print("\n[bold]Run the scanner automatically? (optional)[/bold]")
    slots = scan_slots()
    pruning = prune_slot()
    times = ", ".join(f"{s.hour:02d}:{s.minute:02d}" for s in slots)
    console.print(f"[dim]Scans at {_e(times)} local time. One slot tracks Home Depot's "
                  "3:00 ET Daily Deals refresh, converted to your timezone.[/dim]")
    console.print(f"[dim]A separate job prunes old snapshots at "
                  f"{pruning.hour:02d}:{pruning.minute:02d} — nothing else does.[/dim]")

    if not typer.confirm("  Install the schedule?", default=True):
        return False

    hd_path = hd_executable()
    if hd_path is None:
        console.print(
            "  [yellow]Could not locate the `hd` command, so a scheduled job would "
            "fail silently every time it ran.[/yellow]"
        )
        console.print(
            '  [dim]Install the project into this environment (pip install -e ".") '
            "and rerun setup to schedule it.[/dim]"
        )
        return False

    scan_label = label_for()
    prune_label = f"{scan_label}.prune"

    if not is_macos():
        console.print(
            "\n[dim]  Not macOS — add these crontab lines with `crontab -e`:[/dim]\n"
        )
        console.print(render_crontab(root, hd_path, slots, pruning), markup=False, highlight=False)
        return True

    agents = launch_agents_dir()
    scan_path = write_agent(agents / f"{scan_label}.plist",
                            render_scan_plist(scan_label, root, hd_path, slots))
    prune_path = write_agent(agents / f"{prune_label}.plist",
                             render_prune_plist(prune_label, root, hd_path, pruning))
    console.print(f"  [green]Wrote[/green] {_e(str(scan_path))}")
    console.print(f"  [green]Wrote[/green] {_e(str(prune_path))}")

    if not typer.confirm("  Activate them now?", default=True):
        console.print(f"  [dim]Activate later with: launchctl load {_e(str(scan_path))}[/dim]")
        return True

    for path in (scan_path, prune_path):
        ok, output = await load_agent(path)
        if ok:
            console.print(f"  [green]Loaded[/green] {_e(path.name)}")
        else:
            console.print(f"  [yellow]Could not load {_e(path.name)}: {_e(output)}[/yellow]")
    return True


# ── Verification ──────────────────────────────────────────────────────────────


VERIFY_REQUEST_BUDGET = 15


@dataclass(frozen=True)
class VerifyResult:
    products: int
    snapshots: int
    throttled: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.products > 0


async def verify_install(settings: Settings, store_id: str) -> VerifyResult:
    """Run one small live scan to prove the configuration actually works.

    Deliberately budget-capped: this is a proof of life, not a first harvest.
    Partial coverage is expected and is not a failure — no products at all is.
    """
    from hd.db.base import close_db, init_db
    from hd.pipeline.browse import run_browse

    bounded = settings.model_copy(update={"browse_request_budget": VERIFY_REQUEST_BUDGET})
    try:
        await init_db(bounded)
        summary = await run_browse(settings=bounded, store_ids=[store_id], tiers=("shelf",))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        log.warning("Verification scan failed", error=str(exc))
        return VerifyResult(0, 0, error=str(exc))
    finally:
        # Dispose the engine on every path, or a failed verify leaks it.
        await close_db()

    return VerifyResult(
        products=summary.products, snapshots=summary.snapshots, throttled=summary.aborted
    )


async def _prompt_verify(console, settings: Settings, store_id: str) -> None:
    """Offer a proof-of-life scan and report what actually came back."""
    import typer

    console.print("\n[bold]Check it works?[/bold]")
    console.print(
        f"[dim]Runs a short scan of one store — about {VERIFY_REQUEST_BUDGET} requests, "
        "enough to prove the config is right without a full harvest.[/dim]"
    )
    if not typer.confirm("  Run a test scan?", default=True):
        return

    with console.status("  Scanning..."):
        result = await verify_install(settings, store_id)

    if result.error:
        console.print(f"  [red]Scan failed:[/red] {_e(str(result.error))}")
        console.print("  [dim]The config is written; try `hd run-once` once the issue clears.[/dim]")
        return

    if not result.products:
        console.print(
            "  [yellow]The scan completed but found no products.[/yellow] "
            "That usually means the brand has nothing assorted to this store."
        )
        return

    console.print(
        f"  [green]Working[/green] — {result.products:,} products, "
        f"{result.snapshots:,} snapshots recorded."
    )
    if result.throttled:
        console.print(
            "  [dim]Home Depot throttled the tail of the run, which is normal for a "
            "burst. The scheduled runs are paced.[/dim]"
        )


# ── Database ──────────────────────────────────────────────────────────────────


async def _prompt_database(console, root: Path) -> str:
    """Choose and provision the database. Returns a working DATABASE_URL.

    Runs before the interview so a bad URL costs one question, not all of
    them, and the schema is created here so later steps write into a database
    already proven to work.
    """
    import typer

    from hd.setup_database import (
        SQLITE_DEFAULT,
        check_connection,
        create_database,
        describe,
        initialise_schema,
        redact,
    )

    console.print("\n[bold]Where should the data live?[/bold]")
    console.print(
        "[dim]SQLite is a single file and needs nothing installed — right for one "
        "person watching a few stores. PostgreSQL suits a shared or long-lived "
        "install.[/dim]"
    )

    url = SQLITE_DEFAULT
    if typer.confirm("  Use PostgreSQL instead of SQLite?", default=False):
        url = typer.prompt(
            "  Connection URL",
            default="postgresql+asyncpg://user:password@localhost/hd_monitor",
        ).strip()

    while True:
        check = await check_connection(url)

        if check.missing_database:
            console.print(f"  [yellow]{_e(check.detail)}[/yellow]")
            if typer.confirm("  Create it now?", default=True):
                created = await create_database(url)
                if created.ok:
                    console.print(f"  [green]{_e(created.detail)}[/green]")
                    continue
                console.print(f"  [yellow]{_e(created.detail)}[/yellow]")
                if created.fix:
                    console.print(f"  [dim]{_e(created.fix)}[/dim]")

        elif check.ok:
            console.print(f"  [green]{_e(check.detail)}[/green]")
            break

        else:
            console.print(f"  [yellow]{_e(check.detail)}[/yellow]")
            if check.fix:
                console.print(f"  [dim]{_e(check.fix)}[/dim]")

        console.print(f"  [dim]Tried: {_e(redact(url))}[/dim]")
        if typer.confirm("  Enter a different connection URL?", default=True):
            url = typer.prompt("  Connection URL", default=url).strip()
            continue
        if url != SQLITE_DEFAULT and typer.confirm(
            "  Fall back to a local SQLite file?", default=True
        ):
            url = SQLITE_DEFAULT
            continue
        raise SetupAborted("No usable database")

    try:
        tables = await initialise_schema(Settings(database_url=url))
    except Exception as exc:  # noqa: BLE001 - reported, not a traceback
        console.print(f"  [red]Could not create the schema:[/red] {_e(str(exc))}")
        console.print(
            "  [dim]The connection works, so this is usually a permissions "
            "problem — the role needs CREATE on the schema.[/dim]"
        )
        raise SetupAborted("Schema could not be created") from exc

    console.print(
        f"  [green]Schema ready[/green] — {_e(str(len(tables)))} tables "
        f"in {_e(describe(url))}"
    )
    return url
