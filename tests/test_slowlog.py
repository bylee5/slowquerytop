import io

from sqlalchemy import func, select

from slowquerytop.collectors import mysql_slowlog
from slowquerytop.repository.schema import query_review, query_review_history

SLOWLOG = """/usr/sbin/mysqld, Version: 8.0.36 (MySQL Community Server - GPL). started with:
Tcp port: 3306  Unix socket: /var/run/mysqld/mysqld.sock
Time                 Id Command    Argument
# Time: 2026-11-10T01:15:02.123456Z
# User@Host: app[app] @ web1 [10.0.0.5]  Id:    11
# Query_time: 1.500000  Lock_time: 0.000100 Rows_sent: 10  Rows_examined: 50000
use shop;
SET timestamp=1762737302;
SELECT * FROM orders WHERE customer_id = 17;
# User@Host: app[app] @ web1 [10.0.0.5]  Id:    11
# Query_time: 0.500000  Lock_time: 0.000050 Rows_sent: 5  Rows_examined: 20000
SET timestamp=1762737400;
SELECT * FROM orders WHERE customer_id = 99;
# Time: 2026-11-10T02:00:00Z
# User@Host: batch[batch] @ localhost []  Id:    12
# Schema: crm  Last_errno: 0  Killed: 0
# Query_time: 3.000000  Lock_time: 0.001000 Rows_sent: 0  Rows_examined: 1000000  Rows_affected: 300
SET timestamp=1762740000;
DELETE FROM sessions
 WHERE expires_at < '2026-11-01';
"""

OLD_FORMAT = """# Time: 231114 10:00:00
# User@Host: root[root] @ localhost []
# Query_time: 2  Lock_time: 0  Rows_sent: 1  Rows_examined: 100
select sleep(2);
"""


def test_parse_entries():
    entries = list(mysql_slowlog.parse(io.StringIO(SLOWLOG)))
    assert len(entries) == 3
    e0, e1, e2 = entries
    assert e0.db == "shop" and e0.attrs["Query_time"] == "1.500000"
    assert e0.query == "SELECT * FROM orders WHERE customer_id = 17;"
    assert e1.db == "shop"  # inherited from the thread's earlier 'use'
    assert e2.db == "crm"
    assert "DELETE FROM sessions" in e2.query and "expires_at" in e2.query
    assert e0.ts.isoformat() == "2025-11-10T01:15:02"  # from SET timestamp (UTC)


def test_parse_old_time_format():
    (e,) = list(mysql_slowlog.parse(io.StringIO(OLD_FORMAT)))
    assert e.ts.year == 2023 and e.query == "select sleep(2);"


def test_aggregate_groups_by_fingerprint():
    res = mysql_slowlog.aggregate(mysql_slowlog.parse(io.StringIO(SLOWLOG)), "db1")
    assert len(res.rows) == 3
    assert res.rows[0].checksum == res.rows[1].checksum
    assert res.rows[2].metrics["writes_sum"] == 300


def test_import_is_idempotent_in_replace_mode(engine, tmp_path):
    path = tmp_path / "slow.log"
    path.write_text(SLOWLOG, encoding="utf-8")
    mysql_slowlog.import_file(engine, str(path), "db1")
    mysql_slowlog.import_file(engine, str(path), "db1")
    h = query_review_history.c
    with engine.connect() as conn:
        total = conn.execute(select(func.sum(h.exec_count))).scalar()
        dur = conn.execute(select(func.sum(h.duration_sum))).scalar()
        n_reviews = conn.execute(select(func.count()).select_from(query_review)).scalar()
    assert total == 3
    assert abs(dur - 5.0) < 1e-9
    assert n_reviews == 2


def test_import_add_mode_accumulates(engine, tmp_path):
    path = tmp_path / "slow.log"
    path.write_text(SLOWLOG, encoding="utf-8")
    mysql_slowlog.import_file(engine, str(path), "db1", mode="add")
    mysql_slowlog.import_file(engine, str(path), "db1", mode="add")
    with engine.connect() as conn:
        total = conn.execute(select(func.sum(query_review_history.c.exec_count))).scalar()
        mx = conn.execute(select(func.max(query_review_history.c.duration_max))).scalar()
    assert total == 6
    assert mx == 3.0
