import pytest

from slowquerytop import config

ODBC = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=sql01;Trusted_Connection=yes"


def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_defaults_without_file(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = config.load(None)
    assert cfg.default_datasource == "default" and cfg.targets == []


def test_env_expansion_and_odbc_datasource(tmp_path, monkeypatch):
    monkeypatch.setenv("SQ_PWD", "s3cret")
    path = write(
        tmp_path,
        f"""
datasources:
  repo:
    odbc: 'DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=repo;UID=sq;PWD=${{SQ_PWD}}'
targets:
  - name: a
    odbc: '{ODBC};PWD=${{MISSING:-fallback}}'
""",
    )
    cfg = config.load(path)
    assert cfg.datasources["repo"].url.startswith("mssql+pyodbc:///?odbc_connect=")
    assert "s3cret" in cfg.datasources["repo"].url
    t = cfg.targets[0]
    assert t["odbc"].endswith("PWD=fallback") and t["type"] == "querystore" and t["datasource"] == "repo"


def test_same_server_twice_into_one_datasource_is_rejected(tmp_path):
    path = write(
        tmp_path,
        f"""
targets:
  - {{name: qs, type: querystore, odbc: '{ODBC}'}}
  - {{name: dmv, type: dmv, odbc: '{ODBC}'}}
""",
    )
    with pytest.raises(ValueError, match="double counted"):
        config.load(path)


def test_disjoint_databases_are_allowed(tmp_path):
    path = write(
        tmp_path,
        f"""
targets:
  - {{name: a, odbc: '{ODBC}', databases: [x]}}
  - {{name: b, odbc: '{ODBC}', databases: [y]}}
""",
    )
    assert len(config.load(path).targets) == 2


@pytest.mark.parametrize(
    "body, msg",
    [
        ("targets:\n  - {name: a, type: oracle, odbc: x}\n", "unknown type"),
        ("targets:\n  - {name: a}\n", "odbc"),
        ("targets:\n  - {name: a, odbc: x, datasource: nope}\n", "unknown datasource"),
        ("datasources: {a: {label: x}}\n", "'url' or 'odbc'"),
        ("targets: [\n", "invalid YAML"),
    ],
)
def test_invalid_configs(tmp_path, body, msg):
    with pytest.raises(ValueError, match=msg):
        config.load(write(tmp_path, body))
