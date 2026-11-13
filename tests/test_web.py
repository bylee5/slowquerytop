import base64
import re

import pytest
from fastapi.testclient import TestClient

from slowquerytop.config import Config, Datasource
from slowquerytop.repository import report
from slowquerytop.web.app import create_app


@pytest.fixture()
def client(demo_engine):
    url = str(demo_engine.url)
    cfg = Config(datasources={"demo": Datasource("demo", url, "Demo")}, web={"timezone": "Asia/Seoul"})
    return TestClient(create_app(cfg))


def checksums(html):
    return list(dict.fromkeys(re.findall(r"/query/([0-9A-F]{16})\?", html)))


def test_report_table_and_sorting(client):
    r = client.get("/report")
    assert r.status_code == 200
    assert len(checksums(r.text)) == 12
    r = client.get("/report", params={"order": "exec_count", "dir": "asc", "f": list(report.FIELDS)})
    assert r.status_code == 200 and "▲" in r.text


@pytest.mark.parametrize("group", list(report.GROUPS))
def test_report_groupings(client, group):
    assert client.get("/report", params={"group": group}).status_code == 200


@pytest.mark.parametrize("metric", list(report.GRAPH_METRICS))
def test_graph_view(client, metric):
    r = client.get("/report", params={"view": "graph", "metric": metric, "top": 3})
    assert r.status_code == 200 and 'id="graph-data"' in r.text


def test_query_plan_and_review_flow(client):
    cs = checksums(client.get("/report").text)[0]
    r = client.get(f"/query/{cs}")
    assert r.status_code == 200 and "Execution plans" in r.text
    plan_id = re.findall(r"/plan/(\d+)\?", r.text)[0]
    r = client.get(f"/plan/{plan_id}")
    assert r.status_code == 200 and "Missing index suggestions" in r.text
    r = client.get(f"/plan/{plan_id}/download")
    assert r.status_code == 200 and r.headers["content-disposition"].endswith('.sqlplan"')

    r = client.post(
        f"/query/{cs}/review",
        data={"ds": "demo", "reviewed_by": "홍길동", "comments": "인덱스 추가", "reviewed": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "Review saved" in page and "인덱스 추가" in page
    assert checksums(client.get("/report", params={"status": "reviewed"}).text) == [cs]


def test_hostile_input(client):
    r = client.get("/report", params={"order": "1;drop", "group": "x", "start": "garbage", "limit": "abc", "q": "%' OR 1=1 --"})
    assert r.status_code == 200
    cs = checksums(client.get("/report").text)[0]
    r = client.get(f"/query/{cs}", params={"back": "//evil.example"})
    assert 'href="//evil.example"' not in r.text
    assert client.get("/query/DOESNOTEXIST").status_code == 404
    assert client.get("/plan/999999").status_code == 404


def test_json_api(client):
    r = client.get("/api/report", params={"limit": 3})
    body = r.json()
    assert r.status_code == 200 and len(body["rows"]) == 3 and body["rows"][0]["last_seen"].endswith("Z")
    assert client.get("/api/graph").json()["series"]


def test_basic_auth(demo_engine):
    cfg = Config(
        datasources={"demo": Datasource("demo", str(demo_engine.url))},
        web={"auth": {"username": "dba", "password": "pw"}},
    )
    c = TestClient(create_app(cfg))
    assert c.get("/report").status_code == 401
    token = base64.b64encode(b"dba:pw").decode()
    assert c.get("/report", headers={"Authorization": f"Basic {token}"}).status_code == 200
    bad = base64.b64encode(b"dba:nope").decode()
    assert c.get("/report", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_empty_repository(engine):
    cfg = Config(datasources={"e": Datasource("e", str(engine.url))})
    r = TestClient(create_app(cfg)).get("/report")
    assert r.status_code == 200 and "No data yet" in r.text
