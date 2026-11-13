"""SQL Server showplan XML summarizer (the counterpart of Anemometer's EXPLAIN view)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"


@dataclass
class PlanNode:
    node_id: str
    physical_op: str
    logical_op: str
    est_rows: float | None
    est_cost: float | None  # subtree cost
    cost_pct: float | None
    obj: str
    depth: int
    warnings: list[str] = field(default_factory=list)
    extra: str = ""


@dataclass
class PlanSummary:
    statement: str = ""
    statement_cost: float | None = None
    optimization_level: str = ""
    early_abort: str = ""
    dop: str = ""
    memory_grant_kb: str = ""
    cardinality_model: str = ""
    nodes: list[PlanNode] = field(default_factory=list)
    missing_indexes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parameters: list[dict[str, str]] = field(default_factory=list)
    error: str = ""


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _warnings(el: ET.Element) -> list[str]:
    out: list[str] = []
    w = el.find(f"{NS}Warnings")
    if w is None:
        return out
    for k, v in w.attrib.items():
        if v in ("true", "1"):
            out.append(k)
    for child in w:
        tag = child.tag.replace(NS, "")
        detail = ", ".join(f"{k}={v}" for k, v in child.attrib.items())
        out.append(f"{tag}({detail})" if detail else tag)
    return out


def summarize(xml_text: str) -> PlanSummary:
    s = PlanSummary()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        s.error = f"XML parse error: {exc}"
        return s

    stmt = root.find(f".//{NS}StmtSimple")
    if stmt is not None:
        s.statement = stmt.get("StatementText", "")
        s.statement_cost = _f(stmt.get("StatementSubTreeCost"))
        s.optimization_level = stmt.get("StatementOptmLevel", "")
        s.early_abort = stmt.get("StatementOptmEarlyAbortReason", "")
        s.cardinality_model = stmt.get("CardinalityEstimationModelVersion", "")

    qp = root.find(f".//{NS}QueryPlan")
    if qp is not None:
        s.dop = qp.get("DegreeOfParallelism", "")
        mg = qp.find(f"{NS}MemoryGrantInfo")
        if mg is not None:
            s.memory_grant_kb = mg.get("GrantedMemory", "") or mg.get("SerialDesiredMemory", "")
        s.warnings = _warnings(qp)
        for cr in qp.findall(f"{NS}ParameterList/{NS}ColumnReference"):
            s.parameters.append(
                {
                    "name": cr.get("Column", ""),
                    "compiled": cr.get("ParameterCompiledValue", ""),
                    "runtime": cr.get("ParameterRuntimeValue", ""),
                }
            )

        top = qp.find(f"{NS}RelOp")
        total = s.statement_cost or (_f(top.get("EstimatedTotalSubtreeCost")) if top is not None else None)

        def walk(rel: ET.Element, depth: int) -> None:
            direct = _direct_relops(rel)
            sub_cost = _f(rel.get("EstimatedTotalSubtreeCost"))
            own = sub_cost
            if sub_cost is not None:
                own = sub_cost - sum(_f(c.get("EstimatedTotalSubtreeCost")) or 0 for c in direct)
            pct = (own / total * 100.0) if (own is not None and total) else None
            obj = ""
            extra: list[str] = []
            for el in _own_iter(rel):
                if el.tag == f"{NS}Object" and not obj:
                    obj = _object_text(el)
                elif el.tag in (f"{NS}Predicate", f"{NS}SeekPredicates", f"{NS}SeekPredicateNew"):
                    so = el.find(f".//{NS}ScalarOperator")
                    if so is not None and so.get("ScalarString"):
                        extra.append(so.get("ScalarString", ""))
                    else:
                        # seek predicates: render "col = value" pairs
                        for rng in el.iter(f"{NS}Prefix"):
                            cols = [_colref(c) for c in rng.findall(f"{NS}RangeColumns/{NS}ColumnReference")]
                            vals = [
                                v.get("ScalarString", "")
                                for v in rng.findall(f"{NS}RangeExpressions/{NS}ScalarOperator")
                            ]
                            extra.append(", ".join(f"{c} {rng.get('ScanType', '=')} {v}" for c, v in zip(cols, vals)))
            s.nodes.append(
                PlanNode(
                    node_id=rel.get("NodeId", ""),
                    physical_op=rel.get("PhysicalOp", ""),
                    logical_op=rel.get("LogicalOp", ""),
                    est_rows=_f(rel.get("EstimateRows")),
                    est_cost=sub_cost,
                    cost_pct=max(pct, 0.0) if pct is not None else None,
                    obj=obj,
                    depth=depth,
                    warnings=_warnings(rel),
                    extra=" | ".join(x for x in extra if x)[:300],
                )
            )
            for c in direct:
                walk(c, depth + 1)

        if top is not None:
            walk(top, 0)

    for grp in root.iter(f"{NS}MissingIndexGroup"):
        impact = _f(grp.get("Impact"))
        for mi in grp.findall(f"{NS}MissingIndex"):
            cols: dict[str, list[str]] = {"EQUALITY": [], "INEQUALITY": [], "INCLUDE": []}
            for cg in mi.findall(f"{NS}ColumnGroup"):
                usage = cg.get("Usage", "")
                cols.setdefault(usage, []).extend(c.get("Name", "") for c in cg.findall(f"{NS}Column"))
            table = ".".join(x for x in (mi.get("Database"), mi.get("Schema"), mi.get("Table")) if x)
            key_cols = cols.get("EQUALITY", []) + cols.get("INEQUALITY", [])
            ddl = f"CREATE INDEX [IX_suggested] ON {table} ({', '.join(key_cols)})"
            if cols.get("INCLUDE"):
                ddl += f" INCLUDE ({', '.join(cols['INCLUDE'])})"
            s.missing_indexes.append({"impact": impact, "table": table, "columns": cols, "ddl": ddl})
    return s


def _direct_relops(rel: ET.Element) -> list[ET.Element]:
    out: list[ET.Element] = []

    def visit(el: ET.Element) -> None:
        for child in el:
            if child.tag == f"{NS}RelOp":
                out.append(child)
            else:
                visit(child)

    visit(rel)
    return out


def _own_iter(rel: ET.Element):
    """Descendants of ``rel`` that are not inside a nested RelOp."""
    for child in rel:
        if child.tag == f"{NS}RelOp":
            continue
        yield child
        yield from _own_iter(child)


def _object_text(o: ET.Element) -> str:
    parts = [o.get("Database"), o.get("Schema"), o.get("Table")]
    name = ".".join(p for p in parts if p)
    idx = o.get("Index")
    return f"{name}.{idx}" if idx else name


def _colref(c: ET.Element) -> str:
    return ".".join(x for x in (c.get("Table"), c.get("Column")) if x)
