"""Command line interface for ``dls-deb-girder-alignment``."""

from __future__ import annotations

import json
import logging
import sys
import time
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from pathlib import Path

from . import __version__, config, epics_io
from . import geometry as G

__all__ = ["main"]


def _add_config_args(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "site config YAML. No configuration ships with this package: "
            f"without this, ${config.ENV_CONFIG} is used, then "
            f"{config.DEFAULT_CONFIG_DIR / config.CONFIG_NAME}"
        ),
    )
    parser.add_argument(
        "--serials",
        type=Path,
        default=None,
        help=(
            f"serial -> girder type CSV (default: {config.SERIALS_NAME} beside "
            "the config file)"
        ),
    )
    parser.add_argument(
        "--domain", default=None, help="override epics.domain, e.g. TS02C for bay 2"
    )


def _load(args: Namespace) -> config.Config:
    cfg = config.load(args.config, args.serials)
    if args.domain:
        cfg.domain = args.domain
        prefix = f"{cfg.domain}-{cfg.encoder_device}"
        cfg.pv_map = {
            eid: f"{prefix}:{pv.split(':', 1)[1]}" for eid, pv in cfg.pv_map.items()
        }
    return cfg


def serve(args: Namespace) -> None:
    from .server import Service, create_app

    cfg = _load(args)
    try:
        service = Service(cfg, demo=args.demo)
    except Exception as exc:
        if args.demo:
            raise
        print(
            f"[epics] {exc}\n[epics] falling back to simulated encoders",
            file=sys.stderr,
        )
        service = Service(cfg, demo=True)

    app = create_app(service)
    mode = "DEMO" if service.demo else "LIVE"
    print("=" * 62)
    print(f"  Girder Alignment  -  {mode} mode  -  v{__version__}")
    print(f"  config       : {cfg.config_path}")
    print(f"  domain       : {cfg.domain}  (device prefix {cfg.device_prefix})")
    print(f"  live backend : {service.backend.name}")
    print(f"  geometry rev : {G.GEOMETRY_REVISION}")
    print(f"  serials      : {len(cfg.serials)} in lookup")
    print(f"  sensors      : {len(cfg.sensor_pvs)} temperature PVs")
    print(f"  sessions db  : {cfg.sessions_db}")
    print(f"  reports      : {cfg.reports}")
    print(f"  3D models    : {cfg.models or 'not configured (envelope box only)'}")
    print(f"  open         : http://localhost:{args.port}/")
    # flush: a container's stdout is a pipe, so without this the banner sits in
    # the buffer and the pod looks like it never started.
    print("=" * 62, flush=True)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if args.dev:
        app.run(host=args.host, port=args.port, threaded=True)
    else:
        from waitress import serve as waitress_serve

        waitress_serve(app, host=args.host, port=args.port, threads=8)


def check(args: Namespace) -> None:
    """Prove Channel Access reaches this bay's PVs before an alignment."""
    cfg = _load(args)
    backend = epics_io.make_backend(demo=args.demo)
    enc = epics_io.EncoderService(cfg.pv_map, backend, cfg.scale)
    temp = epics_io.TemperatureService(cfg.sensor_pvs, backend, cfg.max_spread_c)
    print(f"backend: {backend.name}   domain: {cfg.domain}")

    failed = 0
    while True:
        readings = enc.read_all()
        print(f"\n--- encoders ({cfg.device_prefix}) ---")
        for eid in G.ENCODER_IDS:
            r = readings[eid]
            val = "—" if r["absolute"] is None else f"{r['absolute']:+9.4f}"
            flag = "STALE" if r["stale"] else "ok"
            if r["absolute"] is None:
                flag = f"NOT CONNECTED ({r['error']})"
            print(f"  {eid:<9} {r['pv']:<32} {val}  {flag}")
        t = temp.read()
        tv = "—" if t["temperature"] is None else f"{t['temperature']:.2f} C"
        print(
            f"--- temperature: {tv}  [{t['health']}] "
            f"{t.get('sensors_used', 0)}/{t.get('sensors_total', 0)} sensors ---"
        )
        if t.get("failed"):
            print("  failed: " + "; ".join(t["failed"]))
        failed = sum(1 for e in G.ENCODER_IDS if readings[e]["absolute"] is None)
        if not args.watch:
            break
        time.sleep(1.0)
    sys.exit(1 if failed else 0)


def sessions(args: Namespace) -> None:
    """Inspect and prune the session store."""
    from .session import SessionStore

    cfg = _load(args)
    store = SessionStore(cfg.sessions_db)
    if args.action == "list":
        for row in store.list(limit=args.limit):
            print(
                f"{row['id']}  {row['created'][:19]}  {row['serial']:<12} "
                f"{row['gtype']:<3} {row['status']:<9} {row['operator']}"
            )
    elif args.action == "export":
        out = {r["id"]: store.load(r["id"]) for r in store.list(limit=10_000)}
        args.out.write_text(json.dumps(out, indent=2))
        print(f"exported {len(out)} sessions to {args.out}")


def main(args: Sequence[str] | None = None) -> None:
    """Argument parser for the CLI."""
    parser = ArgumentParser(description="Girder alignment service")
    parser.add_argument("-v", "--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="run the web service")
    _add_config_args(p_serve)
    p_serve.add_argument(
        "--demo",
        action="store_true",
        help="simulated encoders - training and off-site use",
    )
    p_serve.add_argument(
        "--host", default="0.0.0.0", help="interface to bind (default: all)"
    )  # noqa: S104
    p_serve.add_argument(
        "--port", type=int, default=8080, help="port to serve on (default: 8080)"
    )
    p_serve.add_argument(
        "--dev", action="store_true", help="use the Flask development server"
    )
    p_serve.set_defaults(func=serve)

    p_check = sub.add_parser("check", help="check Channel Access to this bay")
    _add_config_args(p_check)
    p_check.add_argument(
        "--demo", action="store_true", help="check against the simulator"
    )
    p_check.add_argument(
        "--watch", action="store_true", help="keep polling, to watch an encoder move"
    )
    p_check.set_defaults(func=check)

    p_sess = sub.add_parser("sessions", help="inspect the session store")
    _add_config_args(p_sess)
    p_sess.add_argument("action", choices=["list", "export"], default="list", nargs="?")
    p_sess.add_argument("--limit", type=int, default=50)
    p_sess.add_argument("--out", type=Path, default=Path("sessions.json"))
    p_sess.set_defaults(func=sessions)

    parsed = parser.parse_args(args)
    if not getattr(parsed, "func", None):
        parser.print_help()
        return
    try:
        parsed.func(parsed)
    except FileNotFoundError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
