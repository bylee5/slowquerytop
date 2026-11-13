"""SQL Server plan-cache (DMV) collector — fallback for instances without Query Store.

``sys.dm_exec_query_stats`` holds *cumulative* counters per cached statement. Each run takes a
snapshot, diffs it against the previous snapshot (kept in ``collector_state``) and stores the
delta as one history bucket covering ``[previous run, this run)``.

Caveats (inherent to the DMV): statements evicted from the plan cache between two runs are lost,
and min/max values are lifetime values of the cached plan, not per-interval values.
The very first run only records a baseline.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Engine

from ..repository import store
from .base import CollectResult, HistoryRow, PlanRow
from .fingerprint import fingerprint
from .mssql_common import SYSTEM_DBS, connect, fetch_dicts, hex_hash, server_identity

log = logging.getLogger(__name__)

US = 1_000_000.0

_SQL = """
SELECT
    CONVERT(varchar(20),  qs.query_hash, 1)      AS query_hash,
    CONVERT(varchar(20),  qs.query_plan_hash, 1) AS plan_hash,
    CONVERT(varchar(130), qs.plan_handle, 1)     AS plan_handle,
    qs.statement_start_offset                    AS s_start,
    qs.statement_end_offset                      AS s_end,
    CONVERT(varchar(30), qs.creation_time, 126)  AS created,
    qs.execution_count      AS cnt,
    qs.total_elapsed_time   AS dur,   qs.min_elapsed_time AS dur_min, qs.max_elapsed_time AS dur_max,
    qs.total_worker_time    AS cpu,   qs.min_worker_time  AS cpu_min, qs.max_worker_time  AS cpu_max,
    qs.total_logical_reads  AS lreads, qs.max_logical_reads  AS lreads_max,
    qs.total_physical_reads AS preads, qs.max_physical_reads AS preads_max,
    qs.total_logical_writes AS writes, qs.max_logical_writes AS writes_max,
    qs.total_rows           AS rws,    qs.max_rows           AS rows_max,
    qs.max_grant_kb         AS grant_max,
    DB_NAME(CONVERT(int, pa.value)) AS dbname,
    SUBSTRING(st.text, (qs.statement_start_offset / 2) + 1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
          ELSE qs.statement_end_offset END - qs.statement_start_offset) / 2) + 1) AS stmt
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
OUTER APPLY (SELECT value FROM sys.dm_exec_plan_attributes(qs.plan_handle) WHERE attribute = 'dbid') pa
WHERE qs.execution_count > 0
"""

_PLAN_SQL = """
SELECT query_plan FROM sys.dm_exec_text_query_plan(CONVERT(varbinary(64), ?, 1), ?, ?)
"""

# cumulative counters diffed between snapshots
_COUNTERS = ("cnt", "dur", "cpu", "lreads", "preads", "writes", "rws")


def _key(row: dict[str, Any]) -> str:
    return f"{row['plan_handle']}|{row['s_start']}|{row['s_end']}|{row['created']}"


def collect(engine: Engine, target: dict[str, Any]) -> dict[str, int]:
    conn = connect(target["odbc"], timeout=int(target.get("connect_timeout", 15)))
    try:
        cur = conn.cursor()
        server_name, _ = server_identity(cur)
        hostname = target.get("hostname") or server_name
        wanted = set(target.get("databases") or [])
        include_system = bool(target.get("include_system_databases", False))

        now = store.utcnow()
        rows = fetch_dicts(cur, _SQL)
        prev_time, prev_snap = store.get_state(engine, target["name"], "__dmv__")
        prev_snap = prev_snap or {}

        snapshot: dict[str, list[float]] = {}
        result = CollectResult(source_type="dmv")
        plan_requests: dict[tuple[str, str, str], tuple[str, int, int]] = {}
        max_text = int(target.get("max_text", 20000))

        for row in rows:
            db = row["dbname"] or "(unknown)"
            if not include_system and db in SYSTEM_DBS:
                continue
            if wanted and db not in wanted:
                continue
            k = _key(row)
            cur_vals = [float(row[c] or 0) for c in _COUNTERS]
            snapshot[k] = cur_vals
            if prev_time is None:
                continue  # first run: baseline only
            old = prev_snap.get(k)
            delta = [c - o for c, o in zip(cur_vals, old)] if old else cur_vals
            if delta[0] <= 0:
                continue
            checksum = hex_hash(row["query_hash"])
            plan_hash = hex_hash(row["plan_hash"])
            text = (row["stmt"] or "")[:max_text]
            result.rows.append(
                HistoryRow(
                    hostname=hostname,
                    db=db,
                    checksum=checksum,
                    fingerprint=fingerprint(text, "mssql"),
                    sample=text,
                    ts_min=prev_time,
                    ts_max=now,
                    exec_count=int(delta[0]),
                    plan_hash=plan_hash,
                    metrics={
                        "duration_sum": delta[1] / US,
                        "duration_min": float(row["dur_min"] or 0) / US,
                        "duration_max": float(row["dur_max"] or 0) / US,
                        "cpu_time_sum": delta[2] / US,
                        "cpu_time_min": float(row["cpu_min"] or 0) / US,
                        "cpu_time_max": float(row["cpu_max"] or 0) / US,
                        "logical_reads_sum": delta[3],
                        "logical_reads_max": float(row["lreads_max"] or 0),
                        "physical_reads_sum": delta[4],
                        "physical_reads_max": float(row["preads_max"] or 0),
                        "writes_sum": delta[5],
                        "writes_max": float(row["writes_max"] or 0),
                        "rows_returned_sum": delta[6],
                        "rows_returned_max": float(row["rows_max"] or 0),
                        "memory_grant_kb_max": float(row["grant_max"]) if row["grant_max"] is not None else None,
                    },
                )
            )
            plan_requests.setdefault((db, checksum, plan_hash), (row["plan_handle"], row["s_start"], row["s_end"]))

        if target.get("capture_plans", True) and plan_requests:
            known_by_db: dict[str, set[tuple[str, str]]] = {}
            for (db, checksum, plan_hash), (handle, s, e) in plan_requests.items():
                known = known_by_db.get(db)
                if known is None:
                    known = known_by_db[db] = store.known_plan_hashes(engine, hostname, db)
                if (checksum, plan_hash) in known:
                    continue
                try:
                    prow = fetch_dicts(cur, _PLAN_SQL, handle, s, e)
                except Exception:  # plan may have been evicted meanwhile
                    log.debug("plan fetch failed for %s", handle, exc_info=True)
                    continue
                if prow and prow[0]["query_plan"]:
                    result.plans.append(
                        PlanRow(hostname, db, checksum, plan_hash, prow[0]["query_plan"], now)
                    )
                    known.add((checksum, plan_hash))

        n = store.save_result(engine, result, mode="add")
        store.set_state(engine, target["name"], "__dmv__", now, snapshot)
        if prev_time is None:
            log.info("%s: DMV baseline recorded (%d statements); deltas start next run", target["name"], len(snapshot))
        else:
            log.info("%s: %d history rows, %d new plans", target["name"], n, len(result.plans))
        return {"(all)": n}
    finally:
        conn.close()

