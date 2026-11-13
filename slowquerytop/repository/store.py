"""Write-side access to the repository database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import Engine, and_, create_engine, delete, insert, select, update
from sqlalchemy.engine import Connection

from ..collectors.base import METRIC_KEYS, CollectResult, HistoryRow
from .schema import collector_state, metadata, query_plan, query_review, query_review_history

_engines: dict[str, Engine] = {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_engine(url: str) -> Engine:
    """Return a cached engine for ``url``."""
    eng = _engines.get(url)
    if eng is None:
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("mssql"):
            kwargs["fast_executemany"] = True
        eng = create_engine(url, **kwargs)
        _engines[url] = eng
    return eng


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _merge_rows(rows: Iterable[HistoryRow]) -> list[HistoryRow]:
    merged: dict[tuple, HistoryRow] = {}
    for r in rows:
        cur = merged.get(r.key)
        if cur is None:
            merged[r.key] = HistoryRow(
                hostname=r.hostname,
                db=r.db,
                checksum=r.checksum,
                fingerprint=r.fingerprint,
                sample=r.sample,
                ts_min=r.ts_min,
                ts_max=r.ts_max,
                exec_count=r.exec_count,
                plan_hash=r.plan_hash,
                metrics=dict(r.metrics),
            )
        else:
            cur.merge(r)
    return list(merged.values())


def _upsert_reviews(conn: Connection, rows: list[HistoryRow], source_type: str) -> None:
    by_checksum: dict[str, dict[str, Any]] = {}
    for r in rows:
        info = by_checksum.get(r.checksum)
        if info is None:
            by_checksum[r.checksum] = {
                "checksum": r.checksum,
                "source_type": source_type,
                "fingerprint": r.fingerprint,
                "sample": r.sample,
                "first_seen": r.ts_min,
                "last_seen": r.ts_max,
            }
        else:
            info["first_seen"] = min(info["first_seen"], r.ts_min)
            if r.ts_max > info["last_seen"]:
                info["last_seen"] = r.ts_max
                info["sample"] = r.sample  # keep the most recent sample text

    existing: dict[str, tuple[datetime, datetime]] = {}
    keys = list(by_checksum)
    for chunk in _chunks(keys, 500):
        for row in conn.execute(
            select(query_review.c.checksum, query_review.c.first_seen, query_review.c.last_seen).where(
                query_review.c.checksum.in_(chunk)
            )
        ):
            existing[row.checksum] = (row.first_seen, row.last_seen)

    new = [v for k, v in by_checksum.items() if k not in existing]
    if new:
        conn.execute(insert(query_review), new)

    for k, (first, last) in existing.items():
        info = by_checksum[k]
        values: dict[str, Any] = {}
        if info["first_seen"] < first:
            values["first_seen"] = info["first_seen"]
        if info["last_seen"] > last:
            values["last_seen"] = info["last_seen"]
            values["sample"] = info["sample"]
        if values:
            conn.execute(update(query_review).where(query_review.c.checksum == k).values(**values))


def _history_params(r: HistoryRow) -> dict[str, Any]:
    d: dict[str, Any] = {
        "hostname": r.hostname,
        "db": r.db,
        "checksum": r.checksum,
        "ts_min": r.ts_min,
        "ts_max": r.ts_max,
        "exec_count": int(r.exec_count),
        "plan_hash": r.plan_hash or "",
        "sample": r.sample,
    }
    for k in METRIC_KEYS:
        d[k] = r.metrics.get(k)
    return d


def save_result(engine: Engine, result: CollectResult, mode: str = "replace") -> int:
    """Persist collected rows.

    ``mode='replace'`` overwrites an existing bucket with the same key (idempotent re-collection,
    used by Query Store which re-reports whole intervals). ``mode='add'`` folds the new values
    into an existing bucket (used by incremental sources such as slow-log tailing).
    Returns the number of history rows written.
    """
    rows = _merge_rows(result.rows)
    if not rows and not result.plans:
        return 0
    h = query_review_history.c
    with engine.begin() as conn:
        if rows:
            _upsert_reviews(conn, rows, result.source_type)

            existing: dict[tuple, Any] = {}
            # Look up existing buckets, grouped by (host, db) to keep the IN list on ts_min small.
            groups: dict[tuple[str, str], list[HistoryRow]] = {}
            for r in rows:
                groups.setdefault((r.hostname, r.db), []).append(r)
            for (host, db), grp in groups.items():
                ts_values = sorted({r.ts_min for r in grp})
                checksums = {r.checksum for r in grp}
                for ts_chunk in _chunks(ts_values, 200):
                    q = select(query_review_history).where(
                        and_(h.hostname == host, h.db == db, h.ts_min.in_(ts_chunk))
                    )
                    for row in conn.execute(q).mappings():
                        if row["checksum"] in checksums:
                            existing[(host, db, row["checksum"], row["ts_min"], row["plan_hash"] or "")] = row

            to_insert: list[dict[str, Any]] = []
            for r in rows:
                old = existing.get(r.key)
                if old is None:
                    to_insert.append(_history_params(r))
                    continue
                if mode == "add":
                    prev = HistoryRow(
                        hostname=r.hostname,
                        db=r.db,
                        checksum=r.checksum,
                        fingerprint=r.fingerprint,
                        sample=r.sample,
                        ts_min=old["ts_min"],
                        ts_max=old["ts_max"],
                        exec_count=old["exec_count"],
                        plan_hash=old["plan_hash"] or "",
                        metrics={k: old[k] for k in METRIC_KEYS},
                    )
                    prev.merge(r)
                    r = prev
                params = _history_params(r)
                conn.execute(update(query_review_history).where(h.id == old["id"]).values(**params))
            if to_insert:
                for chunk in _chunks(to_insert, 1000):
                    conn.execute(insert(query_review_history), chunk)

        if result.plans:
            p = query_plan.c
            seen: set[tuple] = set()
            for plan in result.plans:
                key = (plan.hostname, plan.db, plan.checksum, plan.plan_hash)
                if key in seen:
                    continue
                seen.add(key)
                exists = conn.execute(
                    select(p.id).where(
                        and_(
                            p.hostname == plan.hostname,
                            p.db == plan.db,
                            p.checksum == plan.checksum,
                            p.plan_hash == plan.plan_hash,
                        )
                    )
                ).first()
                if exists is None:
                    conn.execute(
                        insert(query_plan).values(
                            checksum=plan.checksum,
                            hostname=plan.hostname,
                            db=plan.db,
                            plan_hash=plan.plan_hash,
                            captured_at=plan.captured_at,
                            plan_xml=plan.plan_xml,
                        )
                    )
    return len(rows)


def known_plan_hashes(engine: Engine, hostname: str, db: str) -> set[tuple[str, str]]:
    p = query_plan.c
    with engine.connect() as conn:
        return {
            (r.checksum, r.plan_hash)
            for r in conn.execute(select(p.checksum, p.plan_hash).where(and_(p.hostname == hostname, p.db == db)))
        }


# ---------------------------------------------------------------- collector state


def get_state(engine: Engine, target: str, scope: str) -> tuple[datetime | None, Any]:
    s = collector_state.c
    with engine.connect() as conn:
        row = conn.execute(
            select(s.watermark, s.snapshot).where(and_(s.target == target, s.scope == scope))
        ).first()
    if row is None:
        return None, None
    return row.watermark, (json.loads(row.snapshot) if row.snapshot else None)


def set_state(engine: Engine, target: str, scope: str, watermark: datetime | None, snapshot: Any = None) -> None:
    s = collector_state.c
    values = {
        "watermark": watermark,
        "snapshot": json.dumps(snapshot) if snapshot is not None else None,
        "updated_at": utcnow(),
    }
    with engine.begin() as conn:
        res = conn.execute(
            update(collector_state).where(and_(s.target == target, s.scope == scope)).values(**values)
        )
        if res.rowcount == 0:
            conn.execute(insert(collector_state).values(target=target, scope=scope, **values))


# ---------------------------------------------------------------- review


def save_review(engine: Engine, checksum: str, reviewed_by: str | None, comments: str | None, reviewed: bool) -> bool:
    r = query_review.c
    values: dict[str, Any] = {
        "reviewed_by": (reviewed_by or None),
        "comments": (comments or None),
        "reviewed_on": utcnow() if reviewed else None,
    }
    with engine.begin() as conn:
        res = conn.execute(update(query_review).where(r.checksum == checksum).values(**values))
        return res.rowcount > 0


def purge_history(engine: Engine, before: datetime) -> int:
    """Delete history rows older than ``before`` (retention)."""
    with engine.begin() as conn:
        res = conn.execute(delete(query_review_history).where(query_review_history.c.ts_max < before))
        return res.rowcount or 0
