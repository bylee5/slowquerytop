"""MySQL / MariaDB / Percona slow query log importer (replaces ``pt-query-digest --review --history``).

Parses the slow log, fingerprints each statement, and aggregates per
(host, db, fingerprint, time-bucket) — the same data Anemometer reads.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import IO, Iterator

from sqlalchemy import Engine

from ..repository import store
from .base import CollectResult, HistoryRow
from .fingerprint import checksum as fp_checksum
from .fingerprint import fingerprint

log = logging.getLogger(__name__)

_TIME_NEW = re.compile(r"^# Time: (\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?")
_TIME_OLD = re.compile(r"^# Time: (\d{6})\s+(\d{1,2}:\d{2}:\d{2})")
_USER_HOST = re.compile(r"^# User@Host: (\S+?)\[[^\]]*\] @ (\S*) ?\[([^\]]*)\](?:\s+Id:\s*(\d+))?")
_KV = re.compile(r"(\w+): (\S+)")
_SET_TS = re.compile(r"^SET timestamp=(\d+);\s*$", re.I)
_USE = re.compile(r"^use ([^;]+);\s*$", re.I)
_PREAMBLE = re.compile(r"^(?:\S+, Version: .*started with:|Tcp port: |Time\s+Id\s+Command\s+Argument)")


@dataclass
class SlowEntry:
    ts: datetime | None = None
    user: str = ""
    client: str = ""
    thread_id: str = ""
    db: str | None = None
    attrs: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    @property
    def query(self) -> str:
        return "\n".join(self.lines).strip()


def _parse_time(line: str) -> datetime | None:
    m = _TIME_NEW.match(line)
    if m:
        ts = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
        tz = m.group(3)
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            hh, mm = tz[1:].split(":")
            ts -= sign * timedelta(hours=int(hh), minutes=int(mm))
        return ts
    m = _TIME_OLD.match(line)
    if m:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%y%m%d %H:%M:%S")
    return None


def parse(stream: IO[str]) -> Iterator[SlowEntry]:
    """Yield one :class:`SlowEntry` per logged statement."""
    cur: SlowEntry | None = None
    in_header = False
    last_ts: datetime | None = None
    db_by_thread: dict[str, str] = {}

    def finish(e: SlowEntry | None) -> Iterator[SlowEntry]:
        if e is not None and e.query:
            if e.db is None and e.thread_id in db_by_thread:
                e.db = db_by_thread[e.thread_id]
            yield e

    for raw in stream:
        line = raw.rstrip("\r\n")
        if _PREAMBLE.match(line):
            continue
        if line.startswith("# "):
            if not in_header:
                # header lines after a statement body start a new entry
                yield from finish(cur)
                cur = SlowEntry(ts=last_ts)
                in_header = True
            assert cur is not None
            t = _parse_time(line)
            if t is not None:
                cur.ts = last_ts = t
                continue
            m = _USER_HOST.match(line)
            if m:
                cur.user, cur.client = m.group(1), (m.group(3) or m.group(2))
                cur.thread_id = m.group(4) or ""
                continue
            for k, v in _KV.findall(line[2:]):
                if k == "Schema" or k == "Database":
                    cur.db = v
                else:
                    cur.attrs[k] = v
            continue
        if cur is None:
            if not line.strip():
                continue
            cur = SlowEntry(ts=last_ts)
        in_header = False
        m = _SET_TS.match(line)
        if m:
            cur.ts = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).replace(tzinfo=None)
            continue
        m = _USE.match(line)
        if m:
            cur.db = m.group(1).strip().strip("`")
            if cur.thread_id:
                db_by_thread[cur.thread_id] = cur.db
            continue
        cur.lines.append(line)
    yield from finish(cur)


def _num(attrs: dict[str, str], key: str) -> float:
    try:
        return float(attrs.get(key, 0) or 0)
    except ValueError:
        return 0.0


def aggregate(
    entries: Iterator[SlowEntry],
    hostname: str,
    bucket_seconds: int = 3600,
    default_db: str = "",
    max_text: int = 20000,
) -> CollectResult:
    result = CollectResult(source_type="mysql_slowlog")
    epoch = datetime(1970, 1, 1)
    for e in entries:
        q = e.query
        fp = fingerprint(q, "mysql")
        if not fp or fp.startswith("set timestamp"):
            continue
        ts = e.ts or store.utcnow()
        secs = int((ts - epoch).total_seconds())
        b = epoch + timedelta(seconds=secs - secs % bucket_seconds)
        qt = _num(e.attrs, "Query_time")
        row = HistoryRow(
            hostname=hostname,
            db=e.db or default_db,
            checksum=fp_checksum(fp),
            fingerprint=fp,
            sample=q[:max_text],
            ts_min=b,
            ts_max=b + timedelta(seconds=bucket_seconds),
            exec_count=1,
            metrics={
                "duration_sum": qt,
                "duration_min": qt,
                "duration_max": qt,
                "lock_time_sum": _num(e.attrs, "Lock_time"),
                "lock_time_max": _num(e.attrs, "Lock_time"),
                "rows_returned_sum": _num(e.attrs, "Rows_sent"),
                "rows_returned_max": _num(e.attrs, "Rows_sent"),
                "rows_examined_sum": _num(e.attrs, "Rows_examined"),
                "rows_examined_max": _num(e.attrs, "Rows_examined"),
            },
        )
        if "Rows_affected" in e.attrs:
            row.metrics["writes_sum"] = _num(e.attrs, "Rows_affected")
            row.metrics["writes_max"] = row.metrics["writes_sum"]
        result.rows.append(row)
    return result


def import_file(
    engine: Engine,
    path: str,
    hostname: str,
    bucket_seconds: int = 3600,
    default_db: str = "",
    mode: str = "replace",
) -> int:
    """Import a slow log file. ``mode='replace'`` makes re-importing the same file idempotent;
    use ``mode='add'`` when importing several logs that cover the same time range."""
    if path == "-":
        import sys

        stream: IO[str] = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
        result = aggregate(parse(stream), hostname, bucket_seconds, default_db)
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            result = aggregate(parse(fh), hostname, bucket_seconds, default_db)
    n = store.save_result(engine, result, mode=mode)
    log.info("%s: %d statements -> %d history rows", path, len(result.rows), n)
    return n
