"""Command line entry point: ``python -m slowquerytop <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import timedelta

from . import config as config_mod
from .repository import store

log = logging.getLogger("slowquerytop")


def _engine(cfg: config_mod.Config, name: str | None):
    ds = cfg.datasource(name)
    eng = store.get_engine(ds.url)
    store.init_db(eng)
    return eng


def cmd_init_db(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    names = [args.datasource] if args.datasource else list(cfg.datasources)
    for name in names:
        _engine(cfg, name)
        print(f"initialized repository schema for datasource '{name}'")
    return 0


def _collect_once(cfg: config_mod.Config, wanted: set[str]) -> int:
    from .collectors import dmv, querystore

    failures = 0
    targets = [t for t in cfg.targets if not wanted or t["name"] in wanted]
    if not targets:
        log.error("no collection targets configured (see 'targets:' in config.yaml)")
        return 1
    for t in targets:
        eng = _engine(cfg, t["datasource"])
        try:
            if t["type"] == "dmv":
                res = dmv.collect(eng, t)
            else:
                res = querystore.collect(eng, t)
            log.info("target %s done: %s", t["name"], res)
        except Exception:
            failures += 1
            log.exception("target %s failed", t["name"])
    if cfg.retention_days > 0:
        cutoff = store.utcnow() - timedelta(days=cfg.retention_days)
        for name in {t["datasource"] for t in targets}:
            n = store.purge_history(_engine(cfg, name), cutoff)
            if n:
                log.info("datasource %s: purged %d history rows older than %s", name, n, cutoff)
    return 1 if failures else 0


def cmd_collect(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    wanted = set(args.target or [])
    if not args.loop:
        return _collect_once(cfg, wanted)
    while True:
        started = time.monotonic()
        _collect_once(cfg, wanted)
        time.sleep(max(1.0, args.loop - (time.monotonic() - started)))


def cmd_import_slowlog(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .collectors import mysql_slowlog

    eng = _engine(cfg, args.datasource)
    total = 0
    for path in args.files:
        total += mysql_slowlog.import_file(
            eng, path, args.hostname, bucket_seconds=args.bucket, default_db=args.db, mode=args.mode
        )
    print(f"imported {len(args.files)} file(s): {total} history rows")
    return 0


def cmd_demo(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from . import demo

    eng = _engine(cfg, args.datasource)
    n = demo.generate(eng, days=args.days)
    print(f"generated {n} demo history rows in datasource '{cfg.datasource(args.datasource).name}'")
    return 0


def cmd_purge(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    eng = _engine(cfg, args.datasource)
    n = store.purge_history(eng, store.utcnow() - timedelta(days=args.days))
    print(f"deleted {n} history rows older than {args.days} days")
    return 0


def cmd_serve(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    for name in cfg.datasources:
        _engine(cfg, name)
    host = args.host or cfg.web.get("host", "127.0.0.1")
    port = args.port or int(cfg.web.get("port", 8080))
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slowquerytop", description="Slow query analyzer (Anemometer for SQL Server)")
    p.add_argument("-c", "--config", help=f"config file (default: ${config_mod.ENV_VAR} or ./config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-db", help="create repository tables")
    s.add_argument("--datasource")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("collect", help="collect statistics from the configured SQL Server targets")
    s.add_argument("-t", "--target", action="append", help="only this target (repeatable)")
    s.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="keep collecting every N seconds")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("import-slowlog", help="import MySQL slow query log file(s) ('-' = stdin)")
    s.add_argument("files", nargs="+")
    s.add_argument("--hostname", required=True, help="server name to file the statistics under")
    s.add_argument("--db", default="", help="database name when the log has no 'use' statements")
    s.add_argument("--bucket", type=int, default=3600, help="time bucket in seconds (default 3600)")
    s.add_argument("--mode", choices=("replace", "add"), default="replace")
    s.add_argument("--datasource")
    s.set_defaults(func=cmd_import_slowlog)

    s = sub.add_parser("demo", help="fill a datasource with synthetic data")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--datasource")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("purge", help="delete history older than N days")
    s.add_argument("--days", type=int, required=True)
    s.add_argument("--datasource")
    s.set_defaults(func=cmd_purge)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = config_mod.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return int(args.func(cfg, args) or 0)


if __name__ == "__main__":
    sys.exit(main())
