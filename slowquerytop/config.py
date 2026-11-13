"""Configuration (YAML). Mirrors Anemometer's ``conf/config.inc.php`` datasources."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import yaml

ENV_VAR = "SLOWQUERYTOP_CONFIG"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in strings (keeps passwords out of the file)."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class Datasource:
    name: str
    url: str
    label: str = ""


@dataclass
class Config:
    datasources: dict[str, Datasource]
    targets: list[dict[str, Any]] = field(default_factory=list)
    web: dict[str, Any] = field(default_factory=dict)
    retention_days: int = 0
    path: str | None = None

    @property
    def default_datasource(self) -> str:
        return next(iter(self.datasources))

    def datasource(self, name: str | None) -> Datasource:
        if name and name in self.datasources:
            return self.datasources[name]
        return self.datasources[self.default_datasource]


DEFAULT_URL = "sqlite:///slowquerytop.db"


def _check_overlap(targets: list[dict[str, Any]]) -> None:
    """Refuse configs where two targets would write the same server/database into one datasource
    (e.g. Query Store *and* DMV for the same instance) — statistics would be counted twice."""
    norm = lambda s: re.sub(r"\s+", "", str(s)).lower()  # noqa: E731
    names = {t["name"] for t in targets}
    if len(names) != len(targets):
        raise ValueError("target names must be unique")
    for i, a in enumerate(targets):
        for b in targets[i + 1 :]:
            if a["datasource"] != b["datasource"]:
                continue
            same_server = norm(a["odbc"]) == norm(b["odbc"]) or (
                a.get("hostname") and a.get("hostname") == b.get("hostname")
            )
            if not same_server:
                continue
            da, db_ = set(a.get("databases") or []), set(b.get("databases") or [])
            if da and db_ and not (da & db_):
                continue
            raise ValueError(
                f"targets {a['name']!r} and {b['name']!r} collect the same server/databases into datasource "
                f"{a['datasource']!r}; statistics would be double counted. Use separate datasources or "
                "disjoint 'databases' lists."
            )


def load(path: str | None = None) -> Config:
    path = path or os.environ.get(ENV_VAR)
    if not path and Path("config.yaml").exists():
        path = "config.yaml"
    raw: dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            try:
                raw = _expand(yaml.safe_load(fh) or {})
            except yaml.YAMLError as exc:
                raise ValueError(f"{path}: invalid YAML: {exc}") from exc

    ds_raw = raw.get("datasources") or {"default": {"url": DEFAULT_URL, "label": "Local"}}
    datasources: dict[str, Datasource] = {}
    for name, v in ds_raw.items():
        if v.get("url"):
            url = v["url"]
        elif v.get("odbc"):  # SQL Server repository given as a plain ODBC connection string
            url = "mssql+pyodbc:///?odbc_connect=" + quote_plus(v["odbc"])
        else:
            raise ValueError(f"datasource {name}: 'url' or 'odbc' is required")
        datasources[name] = Datasource(name=name, url=url, label=v.get("label") or name)
    targets = list(raw.get("targets") or [])
    for t in targets:
        if "name" not in t:
            raise ValueError("every target needs a 'name'")
        t.setdefault("type", "querystore")
        if t["type"] not in ("querystore", "dmv"):
            raise ValueError(f"target {t['name']}: unknown type {t['type']!r} (querystore | dmv)")
        if "odbc" not in t:
            raise ValueError(f"target {t['name']}: 'odbc' connection string is required")
        t.setdefault("datasource", next(iter(datasources)))
        if t["datasource"] not in datasources:
            raise ValueError(f"target {t['name']}: unknown datasource {t['datasource']!r}")
    _check_overlap(targets)
    return Config(
        datasources=datasources,
        targets=targets,
        web=dict(raw.get("web") or {}),
        retention_days=int(raw.get("retention_days") or 0),
        path=path,
    )
