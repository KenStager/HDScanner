"""Tests for database backup snapshots.

Nothing had ever backed up the database: the record lived on one disk,
and a copy of a hot SQLite file taken mid-write is a corrupt database
that still opens. VACUUM INTO is the consistent-snapshot primitive;
these tests pin the verify-then-count and rotation behavior around it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hd.db.base import backup_database


def _make_db(path: Path, rows: int = 3) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)]
    )
    conn.commit()
    conn.close()
    return f"sqlite+aiosqlite:///{path}"


def test_snapshot_is_a_readable_copy(tmp_path):
    url = _make_db(tmp_path / "dev.db")
    dest = tmp_path / "backups"

    path, message = backup_database(url, str(dest), keep=5)

    assert path is not None and path.parent == dest
    assert "verified ok" in message
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    conn.close()


def test_source_file_is_untouched(tmp_path):
    src = tmp_path / "dev.db"
    url = _make_db(src)
    before = src.read_bytes()

    backup_database(url, str(tmp_path / "backups"), keep=5)

    assert src.read_bytes() == before


def test_rotation_keeps_only_the_newest(tmp_path):
    url = _make_db(tmp_path / "dev.db")
    dest = tmp_path / "backups"
    dest.mkdir()
    # Older snapshots, named the way the stamp names them
    for stamp in ("20260101-030000", "20260102-030000", "20260103-030000"):
        (dest / f"dev-{stamp}.db").write_bytes(b"old")
    stranger = dest / "notes.db"
    stranger.write_bytes(b"not ours")

    path, _ = backup_database(url, str(dest), keep=2)

    kept = sorted(p.name for p in dest.glob("dev-*.db"))
    assert len(kept) == 2
    assert path.name in kept
    assert kept[0] == "dev-20260103-030000.db"  # newest old snapshot survives
    assert stranger.exists()  # rotation never touches files it did not name


def test_unavailable_destination_is_a_skip_not_an_error(tmp_path):
    url = _make_db(tmp_path / "dev.db")
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the directory should go")

    path, message = backup_database(url, str(blocker / "backups"), keep=5)

    assert path is None
    assert "destination unavailable" in message


def test_memory_and_foreign_urls_have_nothing_to_back_up(tmp_path):
    for url in ("sqlite+aiosqlite:///:memory:", "postgresql://x/y"):
        path, message = backup_database(url, str(tmp_path), keep=5)
        assert path is None
        assert "nothing to back up" in message


def test_zero_keep_disables_rotation(tmp_path):
    url = _make_db(tmp_path / "dev.db")
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "dev-20260101-030000.db").write_bytes(b"old")

    path, _ = backup_database(url, str(dest), keep=0)

    assert path is not None
    assert (dest / "dev-20260101-030000.db").exists()
