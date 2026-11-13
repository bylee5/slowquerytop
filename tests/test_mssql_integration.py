"""Integration tests against a real SQL Server.

Set ``SLOWQUERYTOP_TEST_ODBC`` to an ODBC connection string of an instance where the login may
create a database (e.g. a local SQL Server Express), otherwise these tests are skipped::

    set SLOWQUERYTOP_TEST_ODBC=DRIVER={ODBC Driver 18 for SQL Server};SERVER=.\\SQLEXPRESS;Trusted_Connection=yes;TrustServerCertificate=yes
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from slowquerytop.collectors import dmv, querystore
from slowquerytop.repository import report, store
from slowquerytop.repository.schema import query_plan, query_review_history

ODBC = os.environ.get("SLOWQUERYTOP_TEST_ODBC")
LAB_DB = "slowquerytop_itest"

pytestmark = pytest.mark.skipif(not ODBC, reason="SLOWQUERYTOP_TEST_ODBC not set")


@pytest.fixture(scope="module")
def lab():
    import pyodbc

    conn = pyodbc.connect(ODBC, autocommit=True)
    cur = conn.cursor()
    cur.execute(f"IF DB_ID('{LAB_DB}') IS NULL CREATE DATABASE {LAB_DB}")
    cur.execute(
        f"ALTER DATABASE {LAB_DB} SET QUERY_STORE = ON "
        "(OPERATION_MODE = READ_WRITE, INTERVAL_LENGTH_MINUTES = 1, QUERY_CAPTURE_MODE = ALL)"
    )
    cur.execute(f"ALTER DATABASE {LAB_DB} SET AUTO_CLOSE OFF")
    cur.execute(f"USE {LAB_DB}")
    cur.execute(
        "IF OBJECT_ID('dbo.T') IS NULL BEGIN "
        "CREATE TABLE dbo.T (id int IDENTITY PRIMARY KEY, k int, v nvarchar(50)); "
        "INSERT INTO dbo.T (k, v) SELECT TOP (20000) ABS(CHECKSUM(NEWID())) % 500, N'값' "
        "FROM sys.all_objects a CROSS JOIN sys.all_objects b; END"
    )
    yield cur
    conn.close()


def _workload(cur, n=15):
    for i in range(n):
        cur.execute(f"SELECT COUNT(*) FROM dbo.T WHERE k = {i}").fetchall()
        cur.execute("SELECT MAX(id) FROM dbo.T WHERE v = ?", "값").fetchall()


def test_querystore_collect(lab, engine):
    _workload(lab)
    target = {"name": "it-qs", "odbc": ODBC, "databases": [LAB_DB], "lookback_hours": 1}
    written = querystore.collect(engine, target)
    assert written[LAB_DB] > 0
    # re-collecting is idempotent
    with engine.connect() as conn:
        before = conn.execute(select(func.sum(query_review_history.c.exec_count))).scalar()
    querystore.collect(engine, target)
    with engine.connect() as conn:
        after = conn.execute(select(func.sum(query_review_history.c.exec_count))).scalar()
        plans = conn.execute(select(func.count()).select_from(query_plan)).scalar()
    assert after == before
    assert plans > 0
    now = store.utcnow()
    rows, totals = report.report_rows(
        engine, report.ReportParams(start=now - timedelta(hours=2), end=now + timedelta(hours=1), text="dbo.T")
    )
    assert rows and totals["exec_count"] >= 15


def test_dmv_collect_baseline_then_delta(lab, engine):
    target = {"name": "it-dmv", "odbc": ODBC, "databases": [LAB_DB]}
    assert dmv.collect(engine, target) == {"(all)": 0}  # baseline
    _workload(lab, n=7)
    dmv.collect(engine, target)
    with engine.connect() as conn:
        total = conn.execute(select(func.sum(query_review_history.c.exec_count))).scalar()
    assert total and total >= 7
