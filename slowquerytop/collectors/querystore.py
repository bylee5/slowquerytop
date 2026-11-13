"""SQL Server Query Store collector (SQL Server 2016+, Azure SQL).

Query Store already aggregates statistics per query/plan/time-interval, which is exactly the
shape of Anemometer's ``global_query_review_history``. We copy those intervals into the
repository, converting units (microseconds -> seconds, 8 KB pages -> KB for memory).

Collection is incremental: per (target, database) we remember the start of the newest interval
seen and re-read from there on the next run, so the still-open interval is refreshed and
closed intervals are never re-read. Rows are written in ``replace`` mode, so re-reading is safe.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine

from ..repository import store
from .base import CollectResult, HistoryRow, PlanRow
from .fingerprint import fingerprint
from .mssql_common import SYSTEM_DBS, connect, fetch_dicts, hex_hash, quote_name, server_identity

log = logging.getLogger(__name__)

US = 1_000_000.0  # microseconds per second

_STATS_SQL = """
SELECT
    CONVERT(varchar(20), q.query_hash, 1)       AS query_hash,
    CONVERT(varchar(20), p.query_plan_hash, 1)  AS plan_hash,
    p.plan_id                                   AS plan_id,
    qt.query_sql_text                           AS query_text,
    CONVERT(datetime2(0), SWITCHOFFSET(i.start_time, '+00:00')) AS ts_min,
    CONVERT(datetime2(0), SWITCHOFFSET(i.end_time,   '+00:00')) AS ts_max,
    rs.count_executions                         AS cnt,
    rs.avg_duration * rs.count_executions       AS duration_sum,
    rs.min_duration                             AS duration_min,
    rs.max_duration                             AS duration_max,
    rs.avg_cpu_time * rs.count_executions       AS cpu_sum,
    rs.min_cpu_time                             AS cpu_min,
    rs.max_cpu_time                             AS cpu_max,
    rs.avg_logical_io_reads * rs.count_executions  AS lreads_sum,
    rs.max_logical_io_reads                     AS lreads_max,
    rs.avg_physical_io_reads * rs.count_executions AS preads_sum,
    rs.max_physical_io_reads                    AS preads_max,
    rs.avg_logical_io_writes * rs.count_executions AS writes_sum,
    rs.max_logical_io_writes                    AS writes_max,
    rs.avg_rowcount * rs.count_executions       AS rows_sum,
    rs.max_rowcount                             AS rows_max,
    rs.max_query_max_used_memory                AS mem_max_pages
    {wait_cols}
FROM {db}.sys.query_store_runtime_stats rs
JOIN {db}.sys.query_store_runtime_stats_interval i
     ON i.runtime_stats_interval_id = rs.runtime_stats_interval_id
JOIN {db}.sys.query_store_plan p ON p.plan_id = rs.plan_id
JOIN {db}.sys.query_store_query q ON q.query_id = p.query_id
JOIN {db}.sys.query_store_query_text qt ON qt.query_text_id = q.query_text_id
{wait_join}
WHERE i.end_time > CAST(? AS datetime2)
  AND rs.count_executions > 0
"""

# Lock waits per plan/interval (Query Store wait stats exist from SQL Server 2017).
_WAIT_COLS = ", w.lock_ms AS lock_ms"
_WAIT_JOIN = """
LEFT JOIN (
    SELECT plan_id, runtime_stats_interval_id, execution_type,
           SUM(total_query_wait_time_ms) AS lock_ms
    FROM {db}.sys.query_store_wait_stats
    WHERE wait_category = 3            -- Lock
    GROUP BY plan_id, runtime_stats_interval_id, execution_type
) w ON w.plan_id = rs.plan_id
   AND w.runtime_stats_interval_id = rs.runtime_stats_interval_id
   AND w.execution_type = rs.execution_type
"""

_PLAN_SQL = "SELECT plan_id, query_plan FROM {db}.sys.query_store_plan WHERE plan_id IN ({ids})"


def list_databases(cur, wanted: list[str] | None) -> list[str]:
    rows = fetch_dicts(
        cur,
        "SELECT name FROM sys.databases WHERE is_query_store_on = 1 AND state_desc = 'ONLINE' ORDER BY name",
    )
    enabled = [r["name"] for r in rows if r["name"] not in SYSTEM_DBS]
    if wanted:
        missing = [d for d in wanted if d not in enabled]
        for d in missing:
            log.warning("database %s: Query Store is not enabled / not online - skipped", d)
        return [d for d in wanted if d in enabled]
    return enabled


def _f(v: Any, scale: float = 1.0) -> float | None:
    return None if v is None else float(v) / scale


def collect_database(
    cur,
    hostname: str,
    db: str,
    since: datetime,
    with_waits: bool,
    known_plans: set[tuple[str, str]],
    max_text: int = 20000,
) -> tuple[CollectResult, datetime | None]:
    qdb = quote_name(db)
    sql = _STATS_SQL.format(
        db=qdb,
        wait_cols=_WAIT_COLS if with_waits else "",
        wait_join=_WAIT_JOIN.format(db=qdb) if with_waits else "",
    )
    rows = fetch_dicts(cur, sql, since)
    result = CollectResult(source_type="querystore")
    newest: datetime | None = None
    need_plans: dict[int, tuple[str, str]] = {}

    for row in rows:
        checksum = hex_hash(row["query_hash"])
        plan_hash = hex_hash(row["plan_hash"])
        text = (row["query_text"] or "")[:max_text]
        lock_ms = row.get("lock_ms")
        result.rows.append(
            HistoryRow(
                hostname=hostname,
                db=db,
                checksum=checksum,
                fingerprint=fingerprint(text, "mssql"),
                sample=text,
                ts_min=row["ts_min"],
                ts_max=row["ts_max"],
                exec_count=int(row["cnt"]),
                plan_hash=plan_hash,
                metrics={
                    "duration_sum": _f(row["duration_sum"], US),
                    "duration_min": _f(row["duration_min"], US),
                    "duration_max": _f(row["duration_max"], US),
                    "cpu_time_sum": _f(row["cpu_sum"], US),
                    "cpu_time_min": _f(row["cpu_min"], US),
                    "cpu_time_max": _f(row["cpu_max"], US),
                    "lock_time_sum": _f(lock_ms or 0, 1000.0) if with_waits else None,
                    "logical_reads_sum": _f(row["lreads_sum"]),
                    "logical_reads_max": _f(row["lreads_max"]),
                    "physical_reads_sum": _f(row["preads_sum"]),
                    "physical_reads_max": _f(row["preads_max"]),
                    "writes_sum": _f(row["writes_sum"]),
                    "writes_max": _f(row["writes_max"]),
                    "rows_returned_sum": _f(row["rows_sum"]),
                    "rows_returned_max": _f(row["rows_max"]),
                    "memory_grant_kb_max": _f(row["mem_max_pages"], 1 / 8.0),
                },
            )
        )
        if newest is None or row["ts_min"] > newest:
            newest = row["ts_min"]
        if (checksum, plan_hash) not in known_plans and row["plan_id"] not in need_plans:
            need_plans[row["plan_id"]] = (checksum, plan_hash)

    now = store.utcnow()
    ids = list(need_plans)
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        plan_sql = _PLAN_SQL.format(db=qdb, ids=",".join(str(int(x)) for x in chunk))
        for prow in fetch_dicts(cur, plan_sql):
            if not prow["query_plan"]:
                continue
            checksum, plan_hash = need_plans[prow["plan_id"]]
            result.plans.append(
                PlanRow(
                    hostname=hostname,
                    db=db,
                    checksum=checksum,
                    plan_hash=plan_hash,
                    plan_xml=prow["query_plan"],
                    captured_at=now,
                )
            )
            known_plans.add((checksum, plan_hash))
    return result, newest


def collect(engine: Engine, target: dict[str, Any]) -> dict[str, int]:
    """Collect all configured databases of one target. Returns rows written per database."""
    lookback = timedelta(hours=float(target.get("lookback_hours", 24)))
    conn = connect(target["odbc"], timeout=int(target.get("connect_timeout", 15)))
    written: dict[str, int] = {}
    try:
        cur = conn.cursor()
        server_name, major = server_identity(cur)
        hostname = target.get("hostname") or server_name
        with_waits = major >= 14
        for db in list_databases(cur, target.get("databases")):
            watermark, _ = store.get_state(engine, target["name"], db)
            since = watermark or (store.utcnow() - lookback)
            known = store.known_plan_hashes(engine, hostname, db) if target.get("capture_plans", True) else None
            try:
                result, newest = collect_database(
                    cur, hostname, db, since, with_waits, known if known is not None else _AlwaysKnown()
                )
            except Exception:
                log.exception("%s/%s: collection failed", target["name"], db)
                continue
            n = store.save_result(engine, result, mode="replace")
            if newest is not None:
                # restart from the newest interval next time, so the open interval is refreshed
                store.set_state(engine, target["name"], db, newest)
            written[db] = n
            log.info("%s/%s: %d history rows, %d new plans", target["name"], db, n, len(result.plans))
    finally:
        conn.close()
    return written


class _AlwaysKnown(set):
    """Plan cache stand-in used when plan capture is disabled."""

    def __contains__(self, item: object) -> bool:  # noqa: D401
        return True
