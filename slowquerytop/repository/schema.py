"""Repository schema.

Mirrors Anemometer's two-table layout produced by ``pt-query-digest --review --history``:

* ``query_review``          ~ ``global_query_review``          (one row per normalized query)
* ``query_review_history``  ~ ``global_query_review_history``  (per host/db/time-bucket statistics)

Additional tables hold captured execution plans and collector state.
All timestamps are stored as naive UTC datetimes; all durations are in seconds.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Unicode,
    UnicodeText,
    UniqueConstraint,
)

metadata = MetaData()

query_review = Table(
    "query_review",
    metadata,
    Column("checksum", String(40), primary_key=True),
    Column("source_type", String(20), nullable=False),  # querystore | dmv | mysql_slowlog
    Column("fingerprint", UnicodeText, nullable=False),
    Column("sample", UnicodeText, nullable=False),
    Column("first_seen", DateTime, nullable=False),
    Column("last_seen", DateTime, nullable=False),
    Column("reviewed_by", Unicode(64)),
    Column("reviewed_on", DateTime),
    Column("comments", UnicodeText),
)

# Metric columns stored for each history row. ``<m>_sum`` is always present; min/max only
# where the source can provide them.
METRICS = {
    # name: (has_min, has_max)
    "duration": (True, True),
    "cpu_time": (True, True),
    "lock_time": (False, True),
    "logical_reads": (False, True),
    "physical_reads": (False, True),
    "writes": (False, True),
    "rows_returned": (False, True),
    "rows_examined": (False, True),
    "memory_grant_kb": (False, True),
}


def _metric_columns() -> list[Column]:
    cols: list[Column] = []
    for name, (has_min, has_max) in METRICS.items():
        cols.append(Column(f"{name}_sum", Float))
        if has_min:
            cols.append(Column(f"{name}_min", Float))
        if has_max:
            cols.append(Column(f"{name}_max", Float))
    return cols


query_review_history = Table(
    "query_review_history",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("hostname", Unicode(128), nullable=False),
    Column("db", Unicode(128), nullable=False),
    Column("checksum", String(40), nullable=False),
    Column("ts_min", DateTime, nullable=False),
    Column("ts_max", DateTime, nullable=False),
    Column("exec_count", BigInteger, nullable=False),
    Column("plan_hash", String(40), nullable=False, default=""),  # "" when unknown
    Column("sample", UnicodeText),
    *_metric_columns(),
    UniqueConstraint("hostname", "db", "checksum", "ts_min", "plan_hash", name="uq_history_bucket"),
)
Index("ix_history_ts", query_review_history.c.ts_min)
Index("ix_history_checksum_ts", query_review_history.c.checksum, query_review_history.c.ts_min)

query_plan = Table(
    "query_plan",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("checksum", String(40), nullable=False),
    Column("hostname", Unicode(128), nullable=False),
    Column("db", Unicode(128), nullable=False),
    Column("plan_hash", String(40), nullable=False),
    Column("captured_at", DateTime, nullable=False),
    Column("plan_xml", UnicodeText, nullable=False),
    UniqueConstraint("hostname", "db", "checksum", "plan_hash", name="uq_plan"),
)
Index("ix_plan_checksum", query_plan.c.checksum)

collector_state = Table(
    "collector_state",
    metadata,
    Column("target", Unicode(128), primary_key=True),
    Column("scope", Unicode(128), primary_key=True),  # e.g. database name
    Column("watermark", DateTime),
    Column("snapshot", UnicodeText),  # JSON blob (DMV collector)
    Column("updated_at", DateTime),
)
