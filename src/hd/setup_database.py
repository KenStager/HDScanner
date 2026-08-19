"""Database provisioning for setup.

Setup has to take an install from nothing to working, and "nothing" includes
the database. For the default SQLite file that is nearly free. For Postgres it
means three separate things can be wrong — the driver is not installed, the
server is unreachable, or the database does not exist yet — and each has a
different fix, so each is reported differently.

The check runs before the interview rather than after it. Seeding used to be
the last step, which meant a bad URL surfaced only once the user had answered
every question, and only after .env had already been written: a traceback and
a half-built install.

Nothing here prints a connection string without redacting it first. A Postgres
URL carries its password.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url

from hd.config import Settings
from hd.logging import get_logger

log = get_logger("setup_database")

SQLITE_DEFAULT = "sqlite+aiosqlite:///./dev.db"

# Async driver each dialect needs, and the extra that installs it.
_DRIVERS = {
    "aiosqlite": ("aiosqlite", None),
    "asyncpg": ("asyncpg", "postgres"),
}


@dataclass
class DbCheck:
    """Outcome of inspecting a database URL."""

    ok: bool
    detail: str = ""
    fix: str = ""
    missing_database: bool = False
    warnings: list[str] = field(default_factory=list)


def redact(url: str) -> str:
    """A connection string safe to print."""
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - unparseable strings are shown as-is
        return url
    return parsed.render_as_string(hide_password=True)


def describe(url: str) -> str:
    """Short human label, e.g. 'SQLite file ./dev.db' or 'PostgreSQL db on host'."""
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001
        return url
    if parsed.get_backend_name() == "sqlite":
        return f"SQLite file {parsed.database or ':memory:'}"
    where = parsed.host or "localhost"
    return f"{parsed.get_backend_name()} database {parsed.database!r} on {where}"


def driver_for(url: str) -> tuple[str | None, str | None]:
    """(module_name, pip_extra) the URL's driver needs, or (None, None)."""
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001
        return None, None
    return _DRIVERS.get(parsed.get_driver_name() or "", (None, None))


def _driver_installed(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None


async def check_connection(url: str) -> DbCheck:
    """Confirm the database can actually be opened.

    Distinguishes "driver missing", "server unreachable" and "database does not
    exist", because only the last one is something setup can fix on its own.
    """
    try:
        parsed = make_url(url)
    except Exception as exc:  # noqa: BLE001
        return DbCheck(False, f"Not a valid database URL: {exc}", "Check DATABASE_URL syntax.")

    module, extra = driver_for(url)
    if module and not _driver_installed(module):
        hint = f'pip install -e ".[{extra}]"' if extra else f"pip install {module}"
        return DbCheck(
            False,
            f"The {module} driver is not installed.",
            f"Install it with: {hint}",
        )

    if parsed.get_backend_name() == "sqlite" and parsed.database:
        parent = Path(parsed.database).expanduser().resolve().parent
        if not parent.exists():
            return DbCheck(False, f"Directory {parent} does not exist.", "Create it, or pick another path.")
        import os

        if not os.access(parent, os.W_OK):
            return DbCheck(False, f"Cannot write to {parent}.", "Choose a writable location.")

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return DbCheck(True, f"Connected to {describe(url)}")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        lowered = message.lower()
        if "does not exist" in lowered or "invalidcatalogname" in lowered:
            return DbCheck(
                False,
                f"The database {parsed.database!r} does not exist yet.",
                "Setup can create it.",
                missing_database=True,
            )
        if "password" in lowered or "authentication" in lowered:
            return DbCheck(False, "The server rejected those credentials.", "Check the user and password.")
        if "connect" in lowered or "refused" in lowered or "timeout" in lowered:
            return DbCheck(
                False,
                f"Could not reach {parsed.host or 'the server'}:{parsed.port or ''}.",
                "Check the server is running and reachable.",
            )
        return DbCheck(False, message.splitlines()[0][:200], "")
    finally:
        await engine.dispose()


async def create_database(url: str) -> DbCheck:
    """CREATE DATABASE for a Postgres URL whose database is missing.

    Connects to the server's maintenance database, which is the only way to
    issue CREATE DATABASE. Needs CREATEDB rights; without them this reports the
    refusal rather than pretending it worked.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        return DbCheck(True, "SQLite creates its file automatically.")

    target = parsed.database
    if not target:
        return DbCheck(False, "No database name in the URL.", "Add one, e.g. .../hd_monitor")

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin_url = parsed.set(database="postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # Identifier, so it cannot be bound as a parameter. Quoted to make
            # an odd-but-legal name safe.
            await conn.execute(text(f'CREATE DATABASE "{target}"'))
        log.info("Created database", database=target)
        return DbCheck(True, f"Created database {target!r}.")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "already exists" in message.lower():
            return DbCheck(True, f"Database {target!r} already exists.")
        if "permission" in message.lower() or "denied" in message.lower():
            return DbCheck(
                False,
                f"This user may not create databases.",
                f"Ask an admin to run: CREATE DATABASE \"{target}\";",
            )
        return DbCheck(False, message.splitlines()[0][:200], "")
    finally:
        await engine.dispose()


async def initialise_schema(settings: Settings) -> list[str]:
    """Create every table the scanner needs. Returns the table names present."""
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from hd.db.base import close_db, init_db
    from hd.db.models import Base

    await init_db(settings)
    await close_db()

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
    finally:
        await engine.dispose()

    expected = set(Base.metadata.tables)
    missing = expected - set(names)
    if missing:
        log.warning("Tables missing after init", missing=sorted(missing))
    return sorted(names)
