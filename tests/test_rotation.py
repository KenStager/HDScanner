"""Tests for page-window rotation across runs."""

from __future__ import annotations

import json

from hd import rotation


def test_first_run_starts_at_page_zero():
    cursors: dict[str, int] = {}
    pages = rotation.next_window(cursors, "kw", "2619", "IN_STORE", slice_pages=4, max_pages=32)
    assert pages == [0, 1, 2, 3]


def test_window_advances_between_runs():
    cursors: dict[str, int] = {}
    first = rotation.next_window(cursors, "kw", "2619", "IN_STORE", 4, 32)
    rotation.advance(cursors, "kw", "2619", "IN_STORE", 4, 32)
    second = rotation.next_window(cursors, "kw", "2619", "IN_STORE", 4, 32)

    assert first != second
    # Deeper pages become reachable on the second run.
    assert max(second) > max(first)


def test_page_zero_always_included():
    """Page 0 carries totalProducts and best-match results; never trade it away."""
    cursors = {"kw|2619|IN_STORE": 20}
    pages = rotation.next_window(cursors, "kw", "2619", "IN_STORE", 4, 32)
    assert 0 in pages


def test_window_wraps_at_max_pages():
    cursors = {"kw|2619|IN_STORE": 30}
    pages = rotation.next_window(cursors, "kw", "2619", "IN_STORE", 4, 32)
    assert all(0 <= p < 32 for p in pages)


def test_cursor_advance_wraps():
    cursors = {"kw|2619|IN_STORE": 30}
    rotation.advance(cursors, "kw", "2619", "IN_STORE", slice_pages=8, max_pages=32)
    assert 0 <= cursors["kw|2619|IN_STORE"] < 32


def test_pages_are_sorted_and_unique():
    cursors = {"kw|2619|IN_STORE": 29}
    pages = rotation.next_window(cursors, "kw", "2619", "IN_STORE", 6, 32)
    assert pages == sorted(set(pages))


def test_keywords_and_stores_rotate_independently():
    cursors: dict[str, int] = {}
    rotation.advance(cursors, "kw-a", "2619", "IN_STORE", 8, 32)
    assert rotation.next_window(cursors, "kw-b", "2619", "IN_STORE", 4, 32) == [0, 1, 2, 3]
    assert rotation.next_window(cursors, "kw-a", "8452", "IN_STORE", 4, 32) == [0, 1, 2, 3]


def test_storefilter_rotates_independently():
    """The IN_STORE and ALL passes must not share a cursor."""
    cursors: dict[str, int] = {}
    rotation.advance(cursors, "kw", "2619", "IN_STORE", 8, 32)
    assert rotation.next_window(cursors, "kw", "2619", "ALL", 3, 3) == [0, 1, 2]


def test_degenerate_inputs_do_not_crash():
    cursors: dict[str, int] = {}
    assert rotation.next_window(cursors, "kw", "2619", "ALL", 0, 32) == [0]
    assert rotation.next_window(cursors, "kw", "2619", "ALL", 4, 0) == [0]
    rotation.advance(cursors, "kw", "2619", "ALL", 4, 0)  # must not raise


def test_roundtrip_persistence(tmp_path):
    p = tmp_path / "cursor.json"
    rotation.save_cursors(str(p), {"kw|2619|IN_STORE": 16})
    assert rotation.load_cursors(str(p)) == {"kw|2619|IN_STORE": 16}


def test_missing_cursor_file_returns_empty(tmp_path):
    assert rotation.load_cursors(str(tmp_path / "nope.json")) == {}


def test_corrupt_cursor_file_degrades_to_empty(tmp_path):
    """Losing coverage beats losing the run."""
    p = tmp_path / "cursor.json"
    p.write_text("{not json")
    assert rotation.load_cursors(str(p)) == {}


def test_non_dict_cursor_file_degrades_to_empty(tmp_path):
    p = tmp_path / "cursor.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert rotation.load_cursors(str(p)) == {}


def test_unwritable_path_does_not_raise(tmp_path):
    rotation.save_cursors(str(tmp_path / "no" / "such" / "dir" / "c.json"), {"a": 1})
