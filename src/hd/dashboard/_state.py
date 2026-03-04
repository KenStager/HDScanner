"""Shared settings reference and pipeline state for dashboard modules."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from hd.config import Settings

settings: Settings | None = None


@dataclass
class PipelineState:
    is_running: bool = False
    last_run_ts: datetime | None = None
    last_run_result: dict | None = None  # {"products": N, "snapshots": N, "alerts": N}
    last_run_error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


pipeline_state = PipelineState()
