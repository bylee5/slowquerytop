"""Read-side reporting queries (the equivalent of Anemometer's report/graph/show_query models).

Everything a user can pick in the UI is looked up in a whitelist (``FIELDS``, ``GROUPS``,
``GRAPH_METRICS``); filter values are always bound parameters, so no user text reaches SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import Engine, and_, case, distinct, func, literal, or_, select
from sqlalchemy.sql import ColumnElement

from .schema import query_plan, query_review, query_review_history

h = query_review_history.c
r = query_review.c


def _nz(expr: ColumnElement) -> ColumnElement:
    return func.nullif(expr, 0)


def _ratio(num: ColumnElement, den: ColumnElement) -> ColumnElement:
    return func.sum(num) * literal(1.0) / _nz(func.sum(den))


@dataclass(frozen=True)
class Field:
    label: str
    fmt: str  # int | sec | num | time | text | pct
    agg: Callable[[], ColumnElement] | None = None  # aggregate over history rows
    review: str | None = None  # column of query_review (checksum grouping only)
    pct_of: str | None = None  # computed in python: share of total of this field
    help: str = ""


def _sum(col: str) -> Callable[[], ColumnElement]:
    return lambda: func.sum(h[col])


def _max(col: str) -> Callable[[], ColumnElement]:
    return lambda: func.max(h[col])


def _min(col: str) -> Callable[[], ColumnElement]:
    return lambda: func.min(h[col])


def _avg(col: str) -> Callable[[], ColumnElement]:
    return lambda: _ratio(h[col], h.exec_count)


FIELDS: dict[str, Field] = {
    # --- identity / review
    "checksum": Field("Checksum", "text"),
    "snippet": Field("Query", "text", review="fingerprint"),
    "hostname": Field("Host", "text", agg=_max("hostname")),
    "db": Field("DB", "text", agg=_max("db")),
    "hosts": Field("Hosts", "int", agg=lambda: func.count(distinct(h.hostname)), help="distinct hosts"),
    "plans": Field("Plans", "int", agg=lambda: func.count(distinct(func.nullif(h.plan_hash, ""))), help="distinct plans"),
    "reviewed_by": Field("Reviewed by", "text", review="reviewed_by"),
    "reviewed_on": Field("Reviewed on", "time", review="reviewed_on"),
    "comments": Field("Comments", "text", review="comments"),
    "first_seen": Field("First seen", "time", agg=_min("ts_min")),
    "last_seen": Field("Last seen", "time", agg=_max("ts_max")),
    # --- counts
    "exec_count": Field("Count", "int", agg=_sum("exec_count"), help="executions (ts_cnt)"),
    "exec_pct": Field("Count %", "pct", pct_of="exec_count"),
    # --- time
    "duration_sum": Field("Time total", "sec", agg=_sum("duration_sum"), help="Query_time_sum"),
    "duration_pct": Field("Time %", "pct", pct_of="duration_sum"),
    "duration_avg": Field("Time avg", "sec", agg=_avg("duration_sum")),
    "duration_min": Field("Time min", "sec", agg=_min("duration_min")),
    "duration_max": Field("Time max", "sec", agg=_max("duration_max")),
    "cpu_time_sum": Field("CPU total", "sec", agg=_sum("cpu_time_sum")),
    "cpu_time_pct": Field("CPU %", "pct", pct_of="cpu_time_sum"),
    "cpu_time_avg": Field("CPU avg", "sec", agg=_avg("cpu_time_sum")),
    "cpu_time_max": Field("CPU max", "sec", agg=_max("cpu_time_max")),
    "lock_time_sum": Field("Lock total", "sec", agg=_sum("lock_time_sum")),
    "lock_time_avg": Field("Lock avg", "sec", agg=_avg("lock_time_sum")),
    # --- io
    "logical_reads_sum": Field("Logical reads", "int", agg=_sum("logical_reads_sum")),
    "logical_reads_pct": Field("Reads %", "pct", pct_of="logical_reads_sum"),
    "logical_reads_avg": Field("Reads avg", "num", agg=_avg("logical_reads_sum")),
    "logical_reads_max": Field("Reads max", "int", agg=_max("logical_reads_max")),
    "physical_reads_sum": Field("Physical reads", "int", agg=_sum("physical_reads_sum")),
    "physical_reads_avg": Field("Phys. reads avg", "num", agg=_avg("physical_reads_sum")),
    "writes_sum": Field("Writes", "int", agg=_sum("writes_sum")),
    "writes_avg": Field("Writes avg", "num", agg=_avg("writes_sum")),
    # --- rows
    "rows_returned_sum": Field("Rows sent", "int", agg=_sum("rows_returned_sum")),
    "rows_returned_avg": Field("Rows sent avg", "num", agg=_avg("rows_returned_sum")),
    "rows_examined_sum": Field("Rows examined", "int", agg=_sum("rows_examined_sum")),
    "rows_examined_avg": Field("Rows exam. avg", "num", agg=_avg("rows_examined_sum")),
    "index_ratio": Field(
        "Index ratio",
        "num",
        agg=lambda: _ratio(h.rows_examined_sum, h.rows_returned_sum),
        help="rows examined / rows sent (MySQL)",
    ),
    "reads_per_row": Field(
        "Reads/row",
        "num",
        agg=lambda: _ratio(h.logical_reads_sum, h.rows_returned_sum),
        help="logical reads / rows returned",
    ),
    "memory_grant_kb_max": Field("Mem grant max KB", "int", agg=_max("memory_grant_kb_max")),
}

GROUPS: dict[str, tuple[str, list[str]]] = {
    "checksum": ("Query", ["checksum"]),
    "checksum_host": ("Query + host/db", ["checksum", "hostname", "db"]),
    "hostname": ("Host", ["hostname"]),
    "db": ("Host + DB", ["hostname", "db"]),
}

DEFAULT_FIELDS = [
    "checksum",
    "snippet",
    "hosts",
    "exec_count",
    "duration_sum",
    "duration_pct",
    "duration_avg",
    "duration_max",
    "cpu_time_avg",
    "logical_reads_avg",
    "rows_returned_avg",
    "last_seen",
    "reviewed_on",
]

GRAPH_METRICS: dict[str, tuple[str, str, str | None]] = {
    # key: (label, sum column, divide-by column -> average)
    "exec_count": ("Executions", "exec_count", None),
    "duration_sum": ("Time total (s)", "duration_sum", None),
    "duration_avg": ("Time avg (s)", "duration_sum", "exec_count"),
    "cpu_time_sum": ("CPU total (s)", "cpu_time_sum", None),
    "cpu_time_avg": ("CPU avg (s)", "cpu_time_sum", "exec_count"),
    "logical_reads_sum": ("Logical reads", "logical_reads_sum", None),
    "logical_reads_avg": ("Logical reads avg", "logical_reads_sum", "exec_count"),
    "physical_reads_sum": ("Physical reads", "physical_reads_sum", None),
    "writes_sum": ("Writes", "writes_sum", None),
    "rows_returned_sum": ("Rows sent", "rows_returned_sum", None),
    "rows_examined_sum": ("Rows examined", "rows_examined_sum", None),
    "lock_time_sum": ("Lock time (s)", "lock_time_sum", None),
}

BUCKETS = {"auto": None, "5min": 300, "hour": 3600, "day": 86400}


@dataclass
class ReportParams:
    start: datetime
    end: datetime
    hostnames: list[str] = field(default_factory=list)
    dbs: list[str] = field(default_factory=list)
    text: str = ""
    checksum: str = ""
    status: str = "all"  # all | unreviewed | reviewed
    min_exec: int = 0
    group: str = "checksum"
    fields: list[str] = field(default_factory=lambda: list(DEFAULT_FIELDS))
    order: str = "duration_sum"
    direction: str = "desc"
    limit: int = 50
    # graph
    metric: str = "duration_sum"
    bucket: str = "auto"
    top: int = 5
    tz_offset: int = 0  # display time zone offset in seconds, for bucket alignment

    def normalized(self) -> "ReportParams":
        if self.group not in GROUPS:
            self.group = "checksum"
        self.fields = [f for f in self.fields if f in FIELDS] or list(DEFAULT_FIELDS)
        if self.order not in FIELDS or FIELDS[self.order].agg is None and self.order != "checksum":
            self.order = "duration_sum"
        self.direction = "asc" if self.direction == "asc" else "desc"
        self.limit = max(1, min(int(self.limit or 50), 5000))
        if self.status not in ("all", "unreviewed", "reviewed"):
            self.status = "all"
        if self.metric not in GRAPH_METRICS:
            self.metric = "duration_sum"
        if self.bucket not in BUCKETS:
            self.bucket = "auto"
        self.top = max(0, min(int(self.top or 0), 20))
        if self.end <= self.start:
            self.end = self.start + timedelta(hours=1)
        return self


def _where(p: ReportParams) -> list[ColumnElement]:
    cond: list[ColumnElement] = [h.ts_min >= p.start, h.ts_min < p.end]
    if p.hostnames:
        cond.append(h.hostname.in_(p.hostnames))
    if p.dbs:
        cond.append(h.db.in_(p.dbs))
    if p.checksum:
        cond.append(h.checksum == p.checksum)
    if p.text:
        like = f"%{_escape_like(p.text)}%"
        cond.append(or_(r.fingerprint.like(like, escape="\\"), r.sample.like(like, escape="\\")))
    if p.status == "reviewed":
        cond.append(r.reviewed_on.is_not(None))
    elif p.status == "unreviewed":
        cond.append(r.reviewed_on.is_(None))
    return cond


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")


def _base_from():
    return query_review_history.join(query_review, query_review.c.checksum == h.checksum)


def report_rows(engine: Engine, p: ReportParams) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Return aggregated rows for the table view plus grand totals (for % columns)."""
    p.normalized()
    group_cols = GROUPS[p.group][1]
    agg_fields = {k: f for k, f in FIELDS.items() if f.agg is not None and k not in group_cols}

    inner_cols = [h[c].label(c) for c in group_cols]
    inner_cols += [f.agg().label(k) for k, f in agg_fields.items()]
    inner = select(*inner_cols).select_from(_base_from()).where(and_(*_where(p))).group_by(*[h[c] for c in group_cols])
    if p.min_exec > 0:
        inner = inner.having(func.sum(h.exec_count) >= p.min_exec)
    sub = inner.subquery("agg")

    outer_cols = list(sub.c)
    from_ = sub
    if "checksum" in group_cols:
        from_ = sub.join(query_review, query_review.c.checksum == sub.c.checksum)
        outer_cols += [
            query_review.c.fingerprint,
            query_review.c.sample.label("sample_text"),
            query_review.c.reviewed_by,
            query_review.c.reviewed_on,
            query_review.c.comments,
        ]
    order_col = sub.c[p.order] if p.order in sub.c else sub.c[group_cols[0]]
    # NULLs last regardless of direction
    nulls = case((order_col.is_(None), 1), else_=0)
    order = order_col.asc() if p.direction == "asc" else order_col.desc()
    stmt = select(*outer_cols).select_from(from_).order_by(nulls, order).limit(p.limit)

    totals_stmt = (
        select(
            func.sum(h.exec_count).label("exec_count"),
            func.sum(h.duration_sum).label("duration_sum"),
            func.sum(h.cpu_time_sum).label("cpu_time_sum"),
            func.sum(h.logical_reads_sum).label("logical_reads_sum"),
            func.count(distinct(h.checksum)).label("queries"),
        )
        .select_from(_base_from())
        .where(and_(*_where(p)))
    )

    with engine.connect() as conn:
        rows = [dict(m) for m in conn.execute(stmt).mappings()]
        t = conn.execute(totals_stmt).mappings().first() or {}
    totals = {k: float(v or 0) for k, v in dict(t).items()}

    for row in rows:
        if "fingerprint" in row:
            row["snippet"] = row.get("fingerprint")
        for k in FIELDS:
            row.setdefault(k, None)
        for k, f in FIELDS.items():
            if f.pct_of:
                tot = totals.get(f.pct_of) or 0
                v = row.get(f.pct_of)
                row[k] = (float(v) / tot * 100.0) if (v is not None and tot) else None
    return rows, totals


# ------------------------------------------------------------------- graph


def _auto_bucket(start: datetime, end: datetime) -> int:
    span = (end - start).total_seconds()
    if span <= 6 * 3600:
        return 300
    if span <= 4 * 86400:
        return 3600
    return 86400


def _floor(ts: datetime, step: int, offset: int = 0) -> datetime:
    """Floor a UTC timestamp to ``step`` seconds, aligned to a time zone ``offset`` (seconds east of UTC)
    so that day buckets start at local midnight."""
    epoch = datetime(1970, 1, 1)
    secs = int((ts - epoch).total_seconds()) + offset
    return epoch + timedelta(seconds=secs - secs % step - offset)


def graph_series(engine: Engine, p: ReportParams) -> dict[str, Any]:
    """Time-series for the graph view.

    Returns the top-N series by the chosen metric (dimension = report group: query, host or db)
    plus an "all" total series when ``top`` is 0.
    """
    p.normalized()
    label, sum_col, div_col = GRAPH_METRICS[p.metric]
    step = BUCKETS[p.bucket] or _auto_bucket(p.start, p.end)
    dim = {"checksum": "checksum", "checksum_host": "checksum", "hostname": "hostname", "db": "db"}[p.group]
    where = and_(*_where(p))

    num = func.sum(h[sum_col])
    den = func.sum(h[div_col]) if div_col else None

    with engine.connect() as conn:
        keys: list[str] = []
        if p.top > 0:
            rank_expr = num * literal(1.0) / _nz(den) if den is not None else num
            q = (
                select(h[dim].label("k"), rank_expr.label("v"))
                .select_from(_base_from())
                .where(where)
                .group_by(h[dim])
                .order_by(case((rank_expr.is_(None), 1), else_=0), rank_expr.desc())
                .limit(p.top)
            )
            keys = [row.k for row in conn.execute(q)]

        cols = [h.ts_min.label("ts"), num.label("num")]
        if den is not None:
            cols.append(den.label("den"))
        if keys:
            q = (
                select(h[dim].label("k"), *cols)
                .select_from(_base_from())
                .where(and_(where, h[dim].in_(keys)))
                .group_by(h[dim], h.ts_min)
            )
        else:
            q = select(literal("all").label("k"), *cols).select_from(_base_from()).where(where).group_by(h.ts_min)
        raw = list(conn.execute(q).mappings())

        snippets: dict[str, str] = {}
        if dim == "checksum" and keys:
            for row in conn.execute(select(r.checksum, r.fingerprint).where(r.checksum.in_(keys))):
                snippets[row.checksum] = row.fingerprint

    # bucket in python (portable across SQLite / SQL Server / MySQL repositories)
    buckets: list[datetime] = []
    t = _floor(p.start, step, p.tz_offset)
    while t < p.end and len(buckets) < 5000:
        buckets.append(t)
        t += timedelta(seconds=step)
    index = {b: i for i, b in enumerate(buckets)}

    acc: dict[str, list[list[float]]] = {}
    for row in raw:
        b = _floor(row["ts"], step, p.tz_offset)
        i = index.get(b)
        if i is None:
            continue
        s = acc.setdefault(str(row["k"]), [[0.0, 0.0] for _ in buckets])
        s[i][0] += float(row["num"] or 0)
        s[i][1] += float(row.get("den") or 0)

    series = []
    order = [str(k) for k in keys] if keys else ["all"]
    for k in order:
        vals = acc.get(k)
        if vals is None:
            data = [None] * len(buckets)
        elif div_col:
            data = [(n / d) if d else None for n, d in vals]
        else:
            data = [n for n, _ in vals]
        name = k
        if k in snippets:
            name = f"{k[:10]} {snippets[k][:60]}"
        series.append({"key": k, "label": name, "data": data})

    return {
        "metric": p.metric,
        "metric_label": label,
        "step": step,
        "labels": [b.strftime("%Y-%m-%d %H:%M") for b in buckets],
        "series": series,
    }


# ------------------------------------------------------------------- single query


def get_review(engine: Engine, checksum: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(select(query_review).where(r.checksum == checksum)).mappings().first()
    return dict(row) if row else None


def list_plans(engine: Engine, checksum: str) -> list[dict[str, Any]]:
    p = query_plan.c
    stmt = (
        select(p.id, p.hostname, p.db, p.plan_hash, p.captured_at)
        .where(p.checksum == checksum)
        .order_by(p.captured_at.desc())
    )
    with engine.connect() as conn:
        return [dict(m) for m in conn.execute(stmt).mappings()]


def get_plan(engine: Engine, plan_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(select(query_plan).where(query_plan.c.id == plan_id)).mappings().first()
    return dict(row) if row else None


def plan_stats(engine: Engine, checksum: str, start: datetime, end: datetime) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Per (host, db, plan) aggregates for one query (helps spot plan regressions)."""
    stmt = (
        select(
            h.hostname,
            h.db,
            h.plan_hash,
            func.sum(h.exec_count).label("exec_count"),
            _ratio(h.duration_sum, h.exec_count).label("duration_avg"),
            _ratio(h.cpu_time_sum, h.exec_count).label("cpu_time_avg"),
            _ratio(h.logical_reads_sum, h.exec_count).label("logical_reads_avg"),
            func.min(h.ts_min).label("first_seen"),
            func.max(h.ts_max).label("last_seen"),
        )
        .where(and_(h.checksum == checksum, h.ts_min >= start, h.ts_min < end))
        .group_by(h.hostname, h.db, h.plan_hash)
    )
    with engine.connect() as conn:
        return {(m["hostname"], m["db"], str(m["plan_hash"])): dict(m) for m in conn.execute(stmt).mappings()}


def dimension_values(engine: Engine) -> dict[str, list[str]]:
    """Distinct hostnames and databases for the filter dropdowns."""
    with engine.connect() as conn:
        hosts = [row[0] for row in conn.execute(select(h.hostname).distinct().order_by(h.hostname))]
        dbs = [row[0] for row in conn.execute(select(h.db).distinct().order_by(h.db))]
    return {"hostnames": hosts, "dbs": dbs}


def data_range(engine: Engine) -> tuple[datetime | None, datetime | None]:
    with engine.connect() as conn:
        row = conn.execute(select(func.min(h.ts_min), func.max(h.ts_max))).first()
    return (row[0], row[1]) if row else (None, None)
