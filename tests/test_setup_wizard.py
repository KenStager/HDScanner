"""Tests for first-run setup.

Two behaviours matter most and are pinned hardest: a .env edit must not
destroy the comments that make the config readable, and a store must reach the
database with its address intact — a store row without city/state/zip silently
loses its store-page links, which is the defect setup exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hd.config import Settings
from hd.hd_api.stores import StoreResult
from hd import setup_wizard as sw
from hd.setup_wizard import (
    EnvFile,
    build_env_values,
    find_stores_for_zip,
    parse_selection,
    preflight,
    seed_stores,
    store_env_value,
)

HADLEY = StoreResult("8452", "Hadley", "Hadley", "MA", "01035", 1.12)
GREENFIELD = StoreResult("2619", "Greenfield", "Greenfield", "MA", "01301", 16.3)
BARE = StoreResult("9999", None, None, None, None)


class FakeBrand:
    def __init__(self, name, token, total):
        self.name, self.token, self.verified_total = name, token, total

    @property
    def config_entry(self):
        return f"{self.name}:{self.token}"


class TestEnvFile:
    SAMPLE = "\n".join([
        "# Database",
        "DATABASE_URL=sqlite+aiosqlite:///./dev.db",
        "",
        "# Crawl settings",
        "STORES=2619,8452",
        "BRANDS=Milwaukee",
    ])

    def _write(self, tmp_path: Path) -> Path:
        p = tmp_path / ".env"
        p.write_text(self.SAMPLE + "\n")
        return p

    def test_missing_file_loads_empty(self, tmp_path):
        env = EnvFile.load(tmp_path / "nope.env")
        assert env.exists is False and env.get("STORES") is None

    def test_reads_values(self, tmp_path):
        env = EnvFile.load(self._write(tmp_path))
        assert env.get("STORES") == "2619,8452"

    def test_edit_preserves_comments_and_order(self, tmp_path):
        env = EnvFile.load(self._write(tmp_path))
        env.set("STORES", "6542")
        out = env.render()
        assert "# Crawl settings" in out
        assert "# Database" in out
        assert "STORES=6542" in out
        assert "STORES=2619,8452" not in out
        # Order is unchanged: STORES still sits under its comment.
        assert out.index("# Crawl settings") < out.index("STORES=6542")

    def test_new_key_is_appended(self, tmp_path):
        env = EnvFile.load(self._write(tmp_path))
        env.set("BRAND_TOKENS", "MILWAUKEE:zv")
        assert env.render().rstrip().endswith("BRAND_TOKENS=MILWAUKEE:zv")

    def test_values_needing_quotes_are_quoted(self, tmp_path):
        env = EnvFile.load(tmp_path / ".env")
        env.set("TITLE", "Deal Board # 1")
        assert env.render().strip() == 'TITLE="Deal Board # 1"'
        assert EnvFile(tmp_path / ".env", env.render().splitlines()).get("TITLE") == "Deal Board # 1"

    def test_plain_values_are_not_quoted(self, tmp_path):
        env = EnvFile.load(tmp_path / ".env")
        env.set("STORES", "2619,8452")
        assert env.render().strip() == "STORES=2619,8452"

    def test_trailing_comment_after_a_value_is_stripped(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("BRANDS=Milwaukee # the good stuff\n")
        assert EnvFile.load(p).get("BRANDS") == "Milwaukee"

    def test_comment_on_a_valueless_key_matches_dotenv(self, tmp_path):
        """python-dotenv treats `KEY=  # note` as the literal comment text.

        Faithfulness matters more than tidiness here: if get() disagreed with
        dotenv, setup would show a value the scanner does not actually use.
        """
        p = tmp_path / ".env"
        p.write_text("EXTRA_NAV_PARAMS=  # additional params\n")
        assert EnvFile.load(p).get("EXTRA_NAV_PARAMS") == "# additional params"

    def test_export_prefix_is_recognised(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("export STORES=1234\n")
        assert EnvFile.load(p).get("STORES") == "1234"

    def test_save_restricts_permissions(self, tmp_path):
        env = EnvFile.load(tmp_path / ".env")
        env.set("SLACK_BOT_TOKEN", "xoxb-secret")
        env.save()
        assert (tmp_path / ".env").stat().st_mode & 0o077 == 0

    def test_similar_key_is_not_confused(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("STORES=1\nSTORES_EXTRA=2\n")
        env = EnvFile.load(p)
        env.set("STORES", "9")
        assert "STORES_EXTRA=2" in env.render()
        assert "STORES=9" in env.render()


class TestParseSelection:
    @pytest.mark.parametrize("raw,expected", [
        ("1", [0]),
        ("1,3", [0, 2]),
        ("3,1", [2, 0]),
        ("1-3", [0, 1, 2]),
        ("1 2", [0, 1]),
        ("2,2", [1]),
        ("3-1", [0, 1, 2]),
    ])
    def test_valid(self, raw, expected):
        assert parse_selection(raw, 5) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "0", "6", "abc", "1,99", "1-9"])
    def test_invalid_raises(self, raw):
        with pytest.raises(ValueError):
            parse_selection(raw, 5)


class TestBuildEnvValues:
    def test_brands_and_tokens_are_written_together(self):
        vals = build_env_values(
            [HADLEY, GREENFIELD],
            [FakeBrand("MILWAUKEE", "zv", 9306), FakeBrand("RYOBI", "m5d", 2419)],
            "M12,M18",
        )
        assert vals["STORES"] == "8452,2619"
        assert vals["BRANDS"] == "MILWAUKEE,RYOBI"
        assert vals["BRAND_TOKENS"] == "MILWAUKEE:zv,RYOBI:m5d"
        assert vals["PRODUCT_LINE_FILTERS"] == "M12,M18"

    def test_brand_and_token_lists_stay_aligned(self):
        vals = build_env_values([HADLEY], [FakeBrand("RYOBI", "m5d", 1)], "")
        names = vals["BRANDS"].split(",")
        tokens = [e.split(":")[0] for e in vals["BRAND_TOKENS"].split(",")]
        assert names == tokens

    def test_store_env_value(self):
        assert store_env_value([HADLEY, GREENFIELD]) == "8452,2619"


class TestFindStoresForZip:
    async def test_returns_first_radius_with_results(self, monkeypatch):
        seen = []

        async def fake(zip_code, *, radius_miles, limit, **kw):
            seen.append(radius_miles)
            return [HADLEY]

        monkeypatch.setattr(sw, "search_stores", fake)
        stores, radius = await find_stores_for_zip("01035")
        assert stores == [HADLEY] and radius == 25.0 and seen == [25.0]

    async def test_widens_radius_when_nothing_nearby(self, monkeypatch):
        seen = []

        async def fake(zip_code, *, radius_miles, limit, **kw):
            seen.append(radius_miles)
            return [GREENFIELD] if radius_miles >= 50.0 else []

        monkeypatch.setattr(sw, "search_stores", fake)
        stores, radius = await find_stores_for_zip("59645")
        assert stores == [GREENFIELD] and radius == 50.0
        assert seen == [25.0, 50.0]

    async def test_empty_when_nothing_at_any_radius(self, monkeypatch):
        async def fake(zip_code, *, radius_miles, limit, **kw):
            return []

        monkeypatch.setattr(sw, "search_stores", fake)
        stores, _ = await find_stores_for_zip("59645")
        assert stores == []


class TestPreflight:
    def test_passes_on_a_writable_directory(self, tmp_path):
        assert preflight(tmp_path, want_dashboard=False).ok is True

    def test_missing_directory_blocks(self, tmp_path):
        result = preflight(tmp_path / "absent", want_dashboard=False)
        assert result.ok is False and result.problems


class TestSeedStores:
    async def test_writes_complete_rows(self, tmp_path):
        from sqlalchemy import select

        from hd.db.base import close_db, get_session
        from hd.db.models import Store

        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", store_raw_json=False
        )
        assert await seed_stores(settings, [HADLEY, BARE]) == 2

        async with get_session(settings) as session:
            rows = {s.store_id: s for s in (await session.execute(select(Store))).scalars()}
        await close_db()

        assert rows["8452"].city == "Hadley"
        assert rows["8452"].zip == "01035"
        assert rows["8452"].state == "MA"
        # A store the API could not describe is still recorded, just bare.
        assert rows["9999"].city is None

    async def test_existing_store_is_enriched_not_duplicated(self, tmp_path):
        from sqlalchemy import select

        from hd.db.base import close_db, get_session, init_db
        from hd.db.models import Store

        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/t2.db", store_raw_json=False
        )
        await init_db(settings)
        async with get_session(settings) as session:
            session.add(Store(store_id="8452"))

        await seed_stores(settings, [HADLEY])
        async with get_session(settings) as session:
            rows = (await session.execute(select(Store))).scalars().all()
        await close_db()

        assert len(rows) == 1
        assert rows[0].city == "Hadley"


class TestVerifyInstall:
    """Setup must not claim success it has not observed."""

    @staticmethod
    def _summary(products=0, snapshots=0, aborted=False):
        from hd.pipeline.browse import BrowseSummary

        return BrowseSummary(products=products, snapshots=snapshots, aborted=aborted)

    async def test_products_found_is_ok(self, monkeypatch, tmp_path):
        async def fake_browse(**kw):
            return self._summary(449, 449)

        monkeypatch.setattr("hd.pipeline.browse.run_browse", fake_browse)
        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/v.db")
        result = await sw.verify_install(settings, "6542")
        assert result.ok and result.products == 449

    async def test_zero_products_is_not_ok(self, monkeypatch, tmp_path):
        async def fake_browse(**kw):
            return self._summary(0, 0)

        monkeypatch.setattr("hd.pipeline.browse.run_browse", fake_browse)
        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/v2.db")
        assert (await sw.verify_install(settings, "6542")).ok is False

    async def test_exception_is_reported_not_swallowed(self, monkeypatch, tmp_path):
        async def fake_browse(**kw):
            raise RuntimeError("network down")

        monkeypatch.setattr("hd.pipeline.browse.run_browse", fake_browse)
        settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/v3.db")
        result = await sw.verify_install(settings, "6542")
        assert result.ok is False and "network down" in result.error

    async def test_scan_is_budget_capped(self, monkeypatch, tmp_path):
        """A proof of life, not a first harvest."""
        seen = {}

        async def fake_browse(*, settings, **kw):
            seen["budget"] = settings.browse_request_budget
            return self._summary(10, 10)

        monkeypatch.setattr("hd.pipeline.browse.run_browse", fake_browse)
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/v4.db", browse_request_budget=280
        )
        await sw.verify_install(settings, "6542")
        assert seen["budget"] == sw.VERIFY_REQUEST_BUDGET


class TestMarkupSafety:
    """Console output must survive text containing square brackets.

    rich parses [...] as markup, so a ZIP, brand name, store name or path with
    brackets either vanishes from the message or raises MarkupError mid-run.
    An unbalanced closing tag is the crash case; a balanced one silently eats
    the text — which produced a wrong `pip install` command in real output.
    """

    HOSTILE = ["[/x]", "[bogus]", "[work]", "[dim]x[/dim]", "[[nested]]"]

    @pytest.mark.parametrize("text", HOSTILE)
    def test_escaped_text_neither_raises_nor_disappears(self, text):
        from rich.console import Console
        from rich.markup import escape

        console = Console(file=__import__("io").StringIO(), width=200, no_color=True)
        console.print(f"  [yellow]value: {escape(text)}[/yellow]")
        out = console.file.getvalue()
        assert text in out, f"{text!r} was swallowed by markup parsing"

    @pytest.mark.parametrize("text", HOSTILE)
    def test_unescaped_text_is_the_bug_being_prevented(self, text):
        """Demonstrates why the escaping matters — do not remove it."""
        import io

        from rich.console import Console
        from rich.errors import MarkupError

        console = Console(file=io.StringIO(), width=200, no_color=True)
        try:
            console.print(f"  [yellow]value: {text}[/yellow]")
        except MarkupError:
            return  # crash path
        assert text not in console.file.getvalue()  # or silent loss

    def test_every_interpolated_print_escapes(self):
        """Static guard: no console.print f-string may interpolate raw text."""
        import re
        from pathlib import Path

        source = Path(sw.__file__).read_text()
        offenders = []
        for line_no, line in enumerate(source.splitlines(), 1):
            if "console.print(f" not in line:
                continue
            for expr in re.findall(r"\{([^{}]+)\}", line):
                expr = expr.strip()
                if expr.startswith("_e(") or ":" in expr.split("!")[0]:
                    continue  # escaped, or a format spec like {n:,} / {r:.0f}
                offenders.append(f"{line_no}: {{{expr}}}")
        assert not offenders, "unescaped interpolations: " + "; ".join(offenders)
