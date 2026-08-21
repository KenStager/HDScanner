"""Tests for Phase 1 request metrics."""

from __future__ import annotations

import json

from hd.http.metrics import RequestMetrics, _percentile


def test_empty_metrics_report_nothing_rather_than_perfect():
    m = RequestMetrics()
    assert m.attempts == 0
    # A zero-request run must not look like a 100% success rate.
    assert m.success_rate == 0.0
    assert m.render() == "No requests made."


def test_success_rate_counts_attempts_not_calls():
    m = RequestMetrics()
    m.record("http_429", 12.0, 429, attempt=1)
    m.record("ok", 340.0, 200, attempt=2)
    # The caller got one usable answer, but two attempts hit the API.
    assert m.attempts == 2
    assert m.successes == 1
    assert m.retries == 1
    assert m.success_rate == 0.5


def test_percentiles_exclude_failures():
    m = RequestMetrics()
    for latency in (100.0, 200.0, 300.0, 400.0):
        m.record("ok", latency, 200)
    # A 30s timeout would swamp p95 without saying anything about API speed.
    m.record("timeout", 30_000.0, None)
    pct = m.latency_percentiles()
    assert pct["p50_ms"] == 200.0
    assert pct["max_ms"] == 400.0
    assert pct["p99_ms"] == 400.0


def test_status_and_outcome_breakdowns():
    m = RequestMetrics()
    m.record("ok", 10.0, 200)
    m.record("ok", 11.0, 200)
    m.record("http_403", 9.0, 403)
    m.record("timeout", 30_000.0, None)
    assert m.by_status() == {"200": 2, "403": 1, "none": 1}
    assert list(m.by_outcome())[0] == "ok"  # sorted by frequency
    assert m.by_outcome()["http_403"] == 1


def test_percentile_of_empty_list_is_zero():
    assert _percentile([], 95) == 0.0


def test_append_jsonl_writes_one_line_per_run(tmp_path):
    path = tmp_path / "nested" / "metrics.jsonl"
    m = RequestMetrics()
    m.record("ok", 250.0, 200)
    m.append_jsonl(path, ts="2026-08-19T00:00:00Z", mode="browse")
    m.append_jsonl(path, ts="2026-08-19T01:00:00Z", mode="browse")

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["mode"] == "browse"
    assert row["success_rate"] == 1.0
    assert row["p50_ms"] == 250.0


def test_append_jsonl_swallows_write_errors(tmp_path):
    # A metrics write must never be able to abort a scan.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    m = RequestMetrics()
    m.record("ok", 1.0, 200)
    m.append_jsonl(blocker / "metrics.jsonl")  # must not raise
