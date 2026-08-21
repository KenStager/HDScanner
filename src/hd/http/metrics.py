"""Per-run request metrics: latency, status codes, and outcomes.

Phase 1 baseline instrumentation. Records one entry per network attempt —
retries included — so the numbers describe what the API actually did rather
than what the caller eventually got back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RequestRecord:
    """One network attempt."""

    outcome: str  # "ok" or a failure reason ("http_429", "timeout", ...)
    latency_ms: float  # time in curl only — excludes rate-limit and backoff sleeps
    status: int | None = None
    attempt: int = 1


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Empty input yields 0.0."""
    if not sorted_values:
        return 0.0
    rank = max(1, min(len(sorted_values), int(round(pct / 100.0 * len(sorted_values)))))
    return sorted_values[rank - 1]


@dataclass
class RequestMetrics:
    """Collects request outcomes for one client's lifetime."""

    records: list[RequestRecord] = field(default_factory=list)

    def record(
        self,
        outcome: str,
        latency_ms: float,
        status: int | None = None,
        attempt: int = 1,
    ) -> None:
        self.records.append(
            RequestRecord(
                outcome=outcome, latency_ms=latency_ms, status=status, attempt=attempt
            )
        )

    @property
    def attempts(self) -> int:
        """Network attempts made, including retries."""
        return len(self.records)

    @property
    def successes(self) -> int:
        return sum(1 for r in self.records if r.outcome == "ok")

    @property
    def retries(self) -> int:
        return sum(1 for r in self.records if r.attempt > 1)

    @property
    def success_rate(self) -> float:
        """Fraction of attempts that returned a usable body. 0.0 when none ran."""
        if not self.records:
            return 0.0
        return self.successes / len(self.records)

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.records:
            key = str(r.status) if r.status is not None else "none"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def by_outcome(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.records:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def latency_percentiles(self) -> dict[str, float]:
        """p50/p95/p99 over successful attempts only.

        Failures are excluded because a 30s timeout and a fast 403 both
        describe the failure path, not how quickly the API answers.
        """
        vals = sorted(r.latency_ms for r in self.records if r.outcome == "ok")
        return {
            "p50_ms": round(_percentile(vals, 50), 1),
            "p95_ms": round(_percentile(vals, 95), 1),
            "p99_ms": round(_percentile(vals, 99), 1),
            "max_ms": round(vals[-1], 1) if vals else 0.0,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "retries": self.retries,
            "success_rate": round(self.success_rate, 4),
            **self.latency_percentiles(),
            "by_status": self.by_status(),
            "by_outcome": self.by_outcome(),
        }

    def render(self) -> str:
        """One-line human summary for the console."""
        s = self.summary()
        if not s["attempts"]:
            return "No requests made."
        outcomes = ", ".join(f"{k}={v}" for k, v in s["by_outcome"].items())
        return (
            f"{s['successes']}/{s['attempts']} ok ({s['success_rate']:.1%}), "
            f"{s['retries']} retried, "
            f"p50 {s['p50_ms']:.0f}ms p95 {s['p95_ms']:.0f}ms p99 {s['p99_ms']:.0f}ms "
            f"[{outcomes}]"
        )

    def append_jsonl(self, path: str | Path, **extra: Any) -> None:
        """Append this run's summary to a JSONL file.

        A single run is too small a sample to characterise the API, so the
        baseline is built by accumulating runs. Write failures are swallowed:
        losing a metrics line must never take down a scan.
        """
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({**extra, **self.summary()}) + "\n")
        except OSError:
            pass
