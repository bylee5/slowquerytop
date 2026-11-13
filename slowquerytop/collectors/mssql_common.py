"""Helpers shared by the SQL Server collectors."""

from __future__ import annotations

from typing import Any

SYSTEM_DBS = ("master", "model", "msdb", "tempdb")


def connect(odbc: str, timeout: int = 15):
    import pyodbc  # imported lazily so the web UI runs without the driver

    conn = pyodbc.connect(odbc, timeout=timeout, autocommit=True)
    conn.timeout = 300  # query timeout (seconds)
    return conn


def fetch_dicts(cur, sql: str, *params: Any) -> list[dict[str, Any]]:
    cur.execute(sql, *params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def quote_name(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def hex_hash(value: str | None) -> str:
    """'0x1A2B..' -> '1A2B..' (upper-case)."""
    if not value:
        return ""
    v = value.strip()
    if v[:2].lower() == "0x":
        v = v[2:]
    return v.upper()


def server_identity(cur) -> tuple[str, int]:
    row = fetch_dicts(
        cur,
        "SELECT CONVERT(nvarchar(256), @@SERVERNAME) AS name, "
        "CONVERT(int, SERVERPROPERTY('ProductMajorVersion')) AS major, "
        "CONVERT(int, SERVERPROPERTY('EngineEdition')) AS edition",
    )[0]
    major = row["major"] or 0
    if row["edition"] in (5, 8):  # Azure SQL DB / Managed Instance: newest feature set
        major = max(major, 99)
    return row["name"] or "unknown", major
