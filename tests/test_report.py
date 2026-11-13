from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from slowquerytop.collectors.base import CollectResult, HistoryRow
from slowquerytop.repository import report, store
from slowquerytop.repository.schema import query_review, query_review_history

T0 = datetime(2026, 11, 1, 0, 0)


def row(checksum="AAA", host="h1", db="d1", hour=0, cnt=10, dur=1.0, plan="P1", text="select 1"):
    return HistoryRow(
        hostname=host,
        db=db,
        checksum=checksum,
        fingerprint=text,
        sample=text,
        ts_min=T0 + timedelta(hours=hour),
        ts_max=T0 + timedelta(hours=hour + 1),
        exec_count=cnt,
        plan_hash=plan,
        metrics={"duration_sum": dur, "duration_max": dur, "logical_reads_sum": cnt * 100.0, "rows_returned_sum": cnt * 2.0},
    )


def params(**kw):
    p = report.ReportParams(start=T0, end=T0 + timedelta(days=1), **kw)
    return p


def test_replace_mode_overwrites_bucket(engine):
    store.save_result(engine, CollectResult("querystore", [row(cnt=10)]))
    store.save_result(engine, CollectResult("querystore", [row(cnt=25, dur=2.0)]))
    with engine.connect() as conn:
        rows = conn.execute(select(query_review_history)).mappings().all()
    assert len(rows) == 1 and rows[0]["exec_count"] == 25 and rows[0]["duration_sum"] == 2.0


def test_rows_in_one_batch_are_merged(engine):
    store.save_result(engine, CollectResult("querystore", [row(cnt=10, dur=1.0), row(cnt=5, dur=3.0)]))
    with engine.connect() as conn:
        r = conn.execute(select(query_review_history)).mappings().one()
    assert r["exec_count"] == 15 and r["duration_sum"] == 4.0 and r["duration_max"] == 3.0


def test_plans_are_separate_buckets(engine):
    store.save_result(engine, CollectResult("querystore", [row(plan="P1"), row(plan="P2")]))
    rows, _ = report.report_rows(engine, params(fields=["checksum", "plans", "exec_count"]))
    assert rows[0]["plans"] == 2 and rows[0]["exec_count"] == 20
    assert set(report.plan_stats(engine, "AAA", T0, T0 + timedelta(days=1))) == {("h1", "d1", "P1"), ("h1", "d1", "P2")}


def test_review_first_last_seen_and_sample(engine):
    store.save_result(engine, CollectResult("querystore", [row(hour=5, text="new text")]))
    store.save_result(engine, CollectResult("querystore", [row(hour=1, text="old text")]))
    store.save_result(engine, CollectResult("querystore", [row(hour=9, text="newest text")]))
    rv = report.get_review(engine, "AAA")
    assert rv["first_seen"] == T0 + timedelta(hours=1)
    assert rv["last_seen"] == T0 + timedelta(hours=10)
    assert rv["sample"] == "newest text"


def _seed(engine):
    store.save_result(
        engine,
        CollectResult(
            "querystore",
            [
                row("AAA", "h1", "d1", 0, cnt=10, dur=8.0, text="select * from orders where id = ?"),
                row("AAA", "h2", "d1", 1, cnt=10, dur=2.0, text="select * from orders where id = ?"),
                row("BBB", "h1", "d2", 2, cnt=100, dur=5.0, text="update stock_100% set x = ?"),
                row("CCC", "h2", "d2", 3, cnt=1, dur=0.5, text="select 1"),
            ],
        ),
    )


def test_report_ordering_and_pct(engine):
    _seed(engine)
    rows, totals = report.report_rows(engine, params())
    assert [r["checksum"] for r in rows] == ["AAA", "BBB", "CCC"]
    assert totals["duration_sum"] == pytest.approx(15.5)
    assert rows[0]["duration_pct"] == pytest.approx(10 / 15.5 * 100)
    assert rows[0]["duration_avg"] == pytest.approx(0.5)
    assert rows[0]["hosts"] == 2
    rows, _ = report.report_rows(engine, params(order="exec_count", direction="asc"))
    assert [r["checksum"] for r in rows] == ["CCC", "AAA", "BBB"]


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({"hostnames": ["h2"]}, {"AAA", "CCC"}),
        ({"dbs": ["d2"]}, {"BBB", "CCC"}),
        ({"text": "orders"}, {"AAA"}),
        ({"text": "100%"}, {"BBB"}),  # LIKE wildcards are escaped
        ({"text": "_"}, {"BBB"}),
        ({"min_exec": 20}, {"AAA", "BBB"}),
        ({"checksum": "CCC"}, {"CCC"}),
    ],
)
def test_report_filters(engine, kw, expected):
    _seed(engine)
    rows, _ = report.report_rows(engine, params(**kw))
    assert {r["checksum"] for r in rows} == expected


def test_review_status_filter(engine):
    _seed(engine)
    assert store.save_review(engine, "BBB", "kim", "add index", True)
    rows, _ = report.report_rows(engine, params(status="reviewed"))
    assert [r["checksum"] for r in rows] == ["BBB"] and rows[0]["reviewed_by"] == "kim"
    rows, _ = report.report_rows(engine, params(status="unreviewed"))
    assert {r["checksum"] for r in rows} == {"AAA", "CCC"}
    assert not store.save_review(engine, "NOPE", "x", "", True)


@pytest.mark.parametrize("group, n", [("checksum", 3), ("checksum_host", 4), ("hostname", 2), ("db", 4)])
def test_groupings(engine, group, n):
    _seed(engine)
    rows, _ = report.report_rows(engine, params(group=group))
    assert len(rows) == n


def test_invalid_params_are_normalized(engine):
    _seed(engine)
    p = params(group="x; drop", order="1; drop table", fields=["nope"], limit=999999, direction="sideways")
    rows, _ = report.report_rows(engine, p)
    assert p.group == "checksum" and p.order == "duration_sum" and p.limit == 5000 and p.direction == "desc"
    assert p.fields == report.DEFAULT_FIELDS and rows


def test_graph_top_series_and_buckets(engine):
    _seed(engine)
    g = report.graph_series(engine, params(metric="exec_count", bucket="hour", top=2))
    assert g["step"] == 3600 and len(g["labels"]) == 24
    assert [s["key"] for s in g["series"]] == ["BBB", "AAA"]
    aaa = g["series"][1]["data"]
    assert aaa[0] == 10 and aaa[1] == 10 and aaa[2] == 0
    g = report.graph_series(engine, params(metric="duration_avg", bucket="day", top=0))
    assert len(g["series"]) == 1 and g["series"][0]["data"][0] == pytest.approx(15.5 / 121)


def test_purge(engine):
    _seed(engine)
    n = store.purge_history(engine, T0 + timedelta(hours=2, minutes=30))
    assert n == 2
    with engine.connect() as conn:
        assert conn.execute(select(query_review)).first() is not None


def test_collector_state_roundtrip(engine):
    assert store.get_state(engine, "t", "db") == (None, None)
    store.set_state(engine, "t", "db", T0, {"k": [1, 2]})
    store.set_state(engine, "t", "db", T0 + timedelta(hours=1), {"k": [3]})
    assert store.get_state(engine, "t", "db") == (T0 + timedelta(hours=1), {"k": [3]})


def test_day_buckets_align_to_display_timezone(engine):
    _seed(engine)
    p = params(metric="exec_count", bucket="day", top=0)
    p.start = T0 - timedelta(days=1)
    p.tz_offset = 9 * 3600  # Asia/Seoul
    g = report.graph_series(engine, p)
    # local midnight 2026-11-01 KST == 2026-10-31 15:00 UTC
    assert g["labels"][1] == "2026-10-31 15:00"
    assert sum(x or 0 for x in g["series"][0]["data"]) == 121
