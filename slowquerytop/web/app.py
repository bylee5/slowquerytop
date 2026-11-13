"""FastAPI web UI (the Anemometer front-end)."""

from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .. import __version__, plan as plan_mod
from ..config import Config
from ..repository import report, store

HERE = Path(__file__).parent


def _safe_back(url: str | None) -> str | None:
    """Only allow local report links as 'back' targets (no open redirects)."""
    if url and url.startswith("/report") and "//" not in url and "\\" not in url:
        return url
    return None


class BasicAuth(BaseHTTPMiddleware):
    def __init__(self, app, username: str, password: str):
        super().__init__(app)
        self.expected = (username.encode(), password.encode())

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).partition(b":")
            except ValueError:
                user, pwd = b"", b""
            if secrets.compare_digest(user, self.expected[0]) and secrets.compare_digest(pwd, self.expected[1]):
                request.state.user = user.decode(errors="replace")
                return await call_next(request)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="slowquerytop"'})


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="slowquerytop", version=__version__, docs_url="/api/docs", redoc_url=None)
    auth = cfg.web.get("auth") or {}
    if auth.get("username") and auth.get("password"):
        app.add_middleware(BasicAuth, username=str(auth["username"]), password=str(auth["password"]))

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    tz = ZoneInfo(cfg.web.get("timezone", "UTC"))
    default_hours = float(cfg.web.get("default_range_hours", 24 * 7))

    # ------------------------------------------------------------ helpers

    def to_local(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)

    def from_local(value: str | None) -> datetime | None:
        if not value:
            return None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                local = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return None
        return local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)

    def fmt_value(value: Any, kind: str) -> str:
        if value is None or value == "":
            return ""
        if kind == "time":
            loc = to_local(value) if isinstance(value, datetime) else value
            return loc.strftime("%Y-%m-%d %H:%M") if isinstance(loc, datetime) else str(loc)
        if kind == "int":
            return f"{float(value):,.0f}"
        if kind == "pct":
            return f"{float(value):.1f}%"
        if kind == "sec":
            v = float(value)
            if v >= 100:
                return f"{v:,.0f}s"
            if v >= 1:
                return f"{v:.2f}s"
            if v >= 0.001:
                return f"{v * 1000:.1f}ms"
            return f"{v * 1_000_000:.0f}µs"
        if kind == "num":
            v = float(value)
            return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
        return str(value)

    templates.env.filters["fmt"] = fmt_value
    templates.env.filters["localtime"] = lambda v: fmt_value(v, "time")
    templates.env.globals.update(
        FIELDS=report.FIELDS,
        GROUPS=report.GROUPS,
        GRAPH_METRICS=report.GRAPH_METRICS,
        BUCKETS=report.BUCKETS,
        datasources=cfg.datasources,
        version=__version__,
        tzname=str(tz),
    )

    def engine_for(ds: str | None):
        return store.get_engine(cfg.datasource(ds).url)

    def params_from(request: Request) -> tuple[report.ReportParams, dict[str, Any]]:
        q = request.query_params
        now = store.utcnow()
        end = from_local(q.get("end")) or (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        start = from_local(q.get("start")) or (end - timedelta(hours=default_hours)).replace(minute=0)

        def as_int(name: str, default: int) -> int:
            try:
                return int(q.get(name, default))
            except (TypeError, ValueError):
                return default

        p = report.ReportParams(
            start=start,
            end=end,
            hostnames=[v for v in q.getlist("host") if v],
            dbs=[v for v in q.getlist("db") if v],
            text=q.get("q", "").strip(),
            checksum=q.get("checksum", "").strip().upper(),
            status=q.get("status", "all"),
            min_exec=as_int("min_exec", 0),
            group=q.get("group", "checksum"),
            fields=q.getlist("f") or list(report.DEFAULT_FIELDS),
            order=q.get("order", "duration_sum"),
            direction=q.get("dir", "desc"),
            limit=as_int("limit", 50),
            metric=q.get("metric", "duration_sum"),
            bucket=q.get("bucket", "auto"),
            top=as_int("top", 5),
        ).normalized()
        off = tz.utcoffset(p.start)
        p.tz_offset = int(off.total_seconds()) if off else 0
        ui = {
            "ds": cfg.datasource(q.get("ds")).name,
            "view": "graph" if q.get("view") == "graph" else "table",
            "start_local": to_local(p.start).strftime("%Y-%m-%dT%H:%M"),
            "end_local": to_local(p.end).strftime("%Y-%m-%dT%H:%M"),
        }
        return p, ui

    def query_string(ui: dict[str, Any], p: report.ReportParams, **override: Any) -> str:
        items: list[tuple[str, Any]] = [
            ("ds", ui["ds"]),
            ("view", ui["view"]),
            ("start", ui["start_local"]),
            ("end", ui["end_local"]),
        ]
        items += [("host", h) for h in p.hostnames] + [("db", d) for d in p.dbs]
        for k, v in (("q", p.text), ("checksum", p.checksum), ("status", p.status), ("min_exec", p.min_exec)):
            if v and v != "all":
                items.append((k, v))
        items += [("group", p.group), ("order", p.order), ("dir", p.direction), ("limit", p.limit)]
        items += [("f", f) for f in p.fields]
        items += [("metric", p.metric), ("bucket", p.bucket), ("top", p.top)]
        d: dict[str, Any] = {}
        for k, v in items:
            d.setdefault(k, []).append(v)
        for k, v in override.items():
            d[k] = [v]
        return urlencode([(k, x) for k, vs in d.items() for x in vs])

    templates.env.globals["query_string"] = query_string

    # ------------------------------------------------------------ pages

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/report", status_code=302)

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request):
        p, ui = params_from(request)
        eng = engine_for(ui["ds"])
        dims = report.dimension_values(eng)
        lo, hi = report.data_range(eng)
        ctx: dict[str, Any] = {
            "request": request,
            "p": p,
            "ui": ui,
            "dims": dims,
            "data_range": (lo, hi),
            "group_cols": report.GROUPS[p.group][1],
        }
        if ui["view"] == "graph":
            ctx["graph"] = report.graph_series(eng, p)
            ctx["graph"]["labels"] = [
                fmt_value(datetime.strptime(x, "%Y-%m-%d %H:%M"), "time") for x in ctx["graph"]["labels"]
            ]
        else:
            rows, totals = report.report_rows(eng, p)
            ctx["rows"], ctx["totals"] = rows, totals
        return templates.TemplateResponse(request, "report.html", ctx)

    @app.get("/query/{checksum}", response_class=HTMLResponse)
    def query_page(request: Request, checksum: str):
        p, ui = params_from(request)
        eng = engine_for(ui["ds"])
        review = report.get_review(eng, checksum)
        if review is None:
            raise HTTPException(404, f"unknown query checksum {checksum}")
        p.checksum = checksum
        p.text, p.status, p.min_exec = "", "all", 0
        p.group, p.order, p.direction, p.limit = "checksum_host", "duration_sum", "desc", 500
        per_host, totals = report.report_rows(eng, p)

        p.top = 0
        p.metric = "exec_count"
        g_cnt = report.graph_series(eng, p)
        p.metric = "duration_avg"
        g_avg = report.graph_series(eng, p)
        p.metric = "logical_reads_avg"
        g_reads = report.graph_series(eng, p)
        labels = [fmt_value(datetime.strptime(x, "%Y-%m-%d %H:%M"), "time") for x in g_cnt["labels"]]
        history = {
            "labels": labels,
            "exec_count": g_cnt["series"][0]["data"],
            "duration_avg": g_avg["series"][0]["data"],
            "logical_reads_avg": g_reads["series"][0]["data"],
        }
        plans = report.list_plans(eng, checksum)
        pstats = report.plan_stats(eng, checksum, p.start, p.end)
        for pl in plans:
            pl["stats"] = pstats.get((pl["hostname"], pl["db"], pl["plan_hash"]))
        captured = {(pl["hostname"], pl["db"], pl["plan_hash"]) for pl in plans}
        orphan_stats = [s for k, s in pstats.items() if k not in captured]
        return templates.TemplateResponse(
            request,
            "query.html",
            {
                "request": request,
                "p": p,
                "ui": ui,
                "review": review,
                "per_host": per_host,
                "totals": totals,
                "history": history,
                "plans": plans,
                "orphan_stats": orphan_stats,
                "saved": request.query_params.get("saved") == "1",
                "back": _safe_back(request.query_params.get("back")) or "/report?" + query_string(ui, p),
            },
        )

    @app.post("/query/{checksum}/review")
    def save_review(
        request: Request,
        checksum: str,
        ds: str = Form(""),
        reviewed_by: str = Form(""),
        comments: str = Form(""),
        reviewed: str = Form(""),
        back: str = Form(""),
    ):
        eng = engine_for(ds)
        user = getattr(request.state, "user", None)
        ok = store.save_review(eng, checksum, (reviewed_by or user or "").strip()[:64], comments.strip(), bool(reviewed))
        if not ok:
            raise HTTPException(404, f"unknown query checksum {checksum}")
        target = f"/query/{checksum}?" + urlencode({"ds": cfg.datasource(ds).name, "saved": 1})
        if _safe_back(back):
            target += "&" + urlencode({"back": back})
        return RedirectResponse(target, status_code=303)

    @app.get("/plan/{plan_id}", response_class=HTMLResponse)
    def plan_page(request: Request, plan_id: int, ds: str = ""):
        eng = engine_for(ds)
        pl = report.get_plan(eng, plan_id)
        if pl is None:
            raise HTTPException(404, "plan not found")
        summary = plan_mod.summarize(pl["plan_xml"])
        return templates.TemplateResponse(
            request,
            "plan.html",
            {"request": request, "plan": pl, "summary": summary, "ui": {"ds": cfg.datasource(ds).name}},
        )

    @app.get("/plan/{plan_id}/download")
    def plan_download(plan_id: int, ds: str = ""):
        pl = report.get_plan(engine_for(ds), plan_id)
        if pl is None:
            raise HTTPException(404, "plan not found")
        return Response(
            pl["plan_xml"].encode("utf-8"),
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{pl["checksum"]}_{pl["plan_hash"]}.sqlplan"'},
        )

    # ------------------------------------------------------------ JSON API

    def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            out.append({k: (v.isoformat() + "Z" if isinstance(v, datetime) else v) for k, v in row.items()})
        return out

    @app.get("/api/report")
    def api_report(request: Request):
        """Same parameters as /report (table view). Timestamps are UTC."""
        p, ui = params_from(request)
        rows, totals = report.report_rows(engine_for(ui["ds"]), p)
        return JSONResponse({"datasource": ui["ds"], "totals": totals, "rows": _jsonable(rows)})

    @app.get("/api/graph")
    def api_graph(request: Request):
        """Same parameters as /report (graph view). Bucket labels are UTC."""
        p, ui = params_from(request)
        return JSONResponse(report.graph_series(engine_for(ui["ds"]), p))

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "version": __version__}

    app.state.config = cfg
    return app
