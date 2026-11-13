"""Query fingerprinting, modelled on pt-query-digest's ``fingerprint()``.

A fingerprint is the query with literals replaced by ``?``, comments removed, whitespace
collapsed and lower-cased, so that executions differing only by parameter values group together.
"""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")
_IN_LIST = re.compile(r"\b(in|values?)\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.I)
_MULTI_VALUES = re.compile(r"\b(values?)\s*\(\?\+\)(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))+", re.I)
_NUM = re.compile(r"(?<![\w.@$#])[-+]?(?:0x[0-9a-f]+|\d+(?:\.\d+)?(?:e[-+]?\d+)?|\.\d+)\b", re.I)
_LIMIT = re.compile(r"\blimit \?(?:, ?\?| offset \?)?", re.I)
_TOP = re.compile(r"\btop\s*\(?\s*\?\s*\)?", re.I)
_MYSQL_ADMIN = re.compile(r"^\s*(?:administrator command:|# administrator command:)", re.I)
_USE = re.compile(r"^\s*use\s+\S+\s*$", re.I)


def _strip_strings_and_comments(sql: str, dialect: str) -> str:
    """Single pass scanner: replace string literals with ``?`` and drop comments.

    Identifiers quoted with backticks, ``[]`` (T-SQL) or ``""`` (T-SQL) are preserved.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    mssql = dialect == "mssql"
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        # comments
        if c == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        if c == "#" and not mssql:
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        if c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            # keep MySQL optimizer hints / versioned comments out of the fingerprint as well
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        # string literals ('..', N'..', and "..." in MySQL)
        if c in ("N", "n") and nxt == "'" and mssql:
            i += 1
            c = "'"
        if c == "'" or (c == '"' and not mssql):
            q = c
            j = i + 1
            while j < n:
                if sql[j] == "\\" and not mssql:
                    j += 2
                    continue
                if sql[j] == q:
                    if j + 1 < n and sql[j + 1] == q:  # doubled quote
                        j += 2
                        continue
                    break
                j += 1
            out.append("?")
            i = j + 1
            continue
        # quoted identifiers: copy verbatim
        if c == "`" or (mssql and c in '["'):
            close = {"`": "`", "[": "]", '"': '"'}[c]
            j = sql.find(close, i + 1)
            j = n - 1 if j < 0 else j
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def fingerprint(sql: str, dialect: str = "mysql") -> str:
    if not sql:
        return ""
    if _MYSQL_ADMIN.match(sql):
        return sql.strip().lower()
    if _USE.match(sql):
        return "use ?"
    s = _strip_strings_and_comments(sql, dialect)
    s = _NUM.sub("?", s)
    s = s.lower()
    s = _WS.sub(" ", s).strip().rstrip(";").strip()
    s = _IN_LIST.sub(lambda m: f"{m.group(1)}(?+)", s)
    s = _MULTI_VALUES.sub(lambda m: f"{m.group(1)}(?+)", s)
    s = _LIMIT.sub("limit ?", s)
    if dialect == "mssql":
        s = _TOP.sub("top ?", s)
    return s


def checksum(fp: str) -> str:
    """pt-query-digest style checksum: low 64 bits of MD5, as 16 hex digits."""
    return hashlib.md5(fp.encode("utf-8")).hexdigest()[-16:].upper()
