"""Common data model produced by every collector."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..repository.schema import METRICS


def metric_keys() -> list[str]:
    keys: list[str] = []
    for name, (has_min, has_max) in METRICS.items():
        keys.append(f"{name}_sum")
        if has_min:
            keys.append(f"{name}_min")
        if has_max:
            keys.append(f"{name}_max")
    return keys


METRIC_KEYS = metric_keys()


@dataclass
class HistoryRow:
    """One (host, db, query, time-bucket) statistics row."""

    hostname: str
    db: str
    checksum: str
    fingerprint: str
    sample: str
    ts_min: datetime
    ts_max: datetime
    exec_count: int
    plan_hash: str = ""
    metrics: dict[str, float | None] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str, datetime, str]:
        return (self.hostname, self.db, self.checksum, self.ts_min, self.plan_hash or "")

    def merge(self, other: "HistoryRow") -> None:
        """Fold ``other`` (same key) into this row."""
        self.exec_count += other.exec_count
        self.ts_max = max(self.ts_max, other.ts_max)
        for k in METRIC_KEYS:
            a, b = self.metrics.get(k), other.metrics.get(k)
            if b is None:
                continue
            if a is None:
                self.metrics[k] = b
            elif k.endswith("_sum"):
                self.metrics[k] = a + b
            elif k.endswith("_min"):
                self.metrics[k] = min(a, b)
            else:
                self.metrics[k] = max(a, b)


@dataclass
class PlanRow:
    hostname: str
    db: str
    checksum: str
    plan_hash: str
    plan_xml: str
    captured_at: datetime


@dataclass
class CollectResult:
    source_type: str
    rows: list[HistoryRow] = field(default_factory=list)
    plans: list[PlanRow] = field(default_factory=list)
