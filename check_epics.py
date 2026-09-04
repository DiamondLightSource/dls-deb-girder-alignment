#!/usr/bin/env python3
"""
EPICS connectivity check — run this before the first live alignment.

Tests, in order: which live backend is available, whether every configured PV
connects and returns a value, whether the bay temperature sensors respond, and
whether the Archiver Appliance answers. Read-only throughout.

    python check_epics.py                 # every configured bay
    python check_epics.py --bay 1
    python check_epics.py --watch         # keep polling, to watch an encoder move

On Windows use pyepics, not cothread: cothread has a C extension with no Windows
wheel, so pip tries to compile it and fails without MSVC build tools.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import epics_io                                     # noqa: E402
from server import load_config                      # noqa: E402

OK, BAD, WARN = "  OK ", " FAIL", " WARN"


def banner(msg):
    print("\n" + msg)
    print("-" * max(56, len(msg)))


def check_ca_environment():
    """Channel Access needs to be able to reach the IOC's subnet."""
    import os
    banner("1b. Channel Access environment")
    addr = os.environ.get("EPICS_CA_ADDR_LIST")
    auto = os.environ.get("EPICS_CA_AUTO_ADDR_LIST", "YES")
    if addr:
        print(f"{OK}  EPICS_CA_ADDR_LIST = {addr}")
    else:
        print(f"{WARN}  EPICS_CA_ADDR_LIST is not set")
        print("        CA will only broadcast on this machine's own subnets. If the")
        print("        IOC is elsewhere (usual for an office PC) nothing will connect.")
        print("        Set it to the IOC host or its broadcast address, e.g.:")
        print('            Windows :  set EPICS_CA_ADDR_LIST=172.23.240.255')
        print('            Linux   :  export EPICS_CA_ADDR_LIST=172.23.240.255')
    print(f"        EPICS_CA_AUTO_ADDR_LIST = {auto}")
    try:
        import epics, os.path
        d = os.path.join(os.path.dirname(epics.__file__), "clibs")
        print(f"        pyepics libs: {d}")
        print("        (the 'caRepeater' warning is harmless — it is a helper daemon,")
        print("         not required for reads)")
    except Exception:
        pass
    return bool(addr)


def probe_hosts(pvs, label=""):
    """Report which IOC (host:port) actually serves each PV.

    This is the diagnostic that settles 'why does caget work for some PVs but
    not others': a PV that resolves prints the IOC serving it, one that does not
    resolve was never answered by anything. Comparing a working PV's host with
    the subnet of the one that fails tells you exactly what to put in
    EPICS_CA_ADDR_LIST — or what to ask Controls for.
    """
    try:
        import epics
    except ImportError:
        print(f"{WARN}  pyepics not installed — cannot probe hosts")
        return
    banner(f"1c. Which IOC serves each PV{(' — ' + label) if label else ''}")
    for name in pvs:
        pv = epics.PV(name, connection_timeout=2.0)
        pv.wait_for_connection(timeout=2.0)
        if pv.connected:
            host = getattr(pv, "host", None) or "?"
            print(f"{OK}  {name}")
            print(f"        served by {host}")
        else:
            print(f"{BAD}  {name}")
            print("        no IOC answered the search — either the name is wrong,")
            print("        or no IOC on a reachable subnet hosts it")
        pv.disconnect()


def find_pvs(cfg, pattern):
    """Ask the Archiver Appliance which PVs match a pattern.

    Useful precisely when CA is blocked: the archiver is reachable over plain
    HTTP, so it can confirm whether a PV name is real and whether it is being
    archived. That separates "wrong name" from "cannot route to the IOC", and
    tells you whether the archiver fallback is even an option for the encoders.
    """
    import json as _json
    import urllib.parse
    import urllib.request

    banner(f"Archiver PV lookup — {pattern}")
    ep = cfg.get("epics", {})
    hosts = []
    if ep.get("archiver_rest_host"):
        hosts.append((ep["archiver_rest_host"], int(ep.get("archiver_rest_port", 80))))
    if ep.get("archiver_host"):
        hosts.append((ep["archiver_host"], int(ep.get("archiver_port", 80))))

    for host, port in hosts:
        url = (f"http://{host}:{port}/mgmt/bpl/getAllPVs?"
               + urllib.parse.urlencode({"pv": pattern, "limit": "200"}))
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                pvs = _json.loads(r.read().decode())
        except Exception as exc:
            print(f"{WARN}  {host}:{port} — {str(exc)[:60]}")
            continue
        if not pvs:
            print(f"{WARN}  {host}:{port} — no PVs match {pattern!r}")
            print("        Either the name/prefix is wrong, or these PVs are not archived.")
            continue
        print(f"{OK}  {host}:{port} — {len(pvs)} matching PV(s):")
        for name in sorted(pvs)[:40]:
            print(f"        {name}")
        if len(pvs) > 40:
            print(f"        … and {len(pvs)-40} more")
        print("\n        The names are real and archived, so a CA failure for these is")
        print("        a ROUTING problem, not a typo.")
        return pvs
    print(f"{BAD}  no archiver answered — check the hosts in config.yaml")
    return []


def check_backends():
    banner("1. Live backend availability")
    found = []
    for name, mod, note in [
        ("cothread", "cothread.catools", "Diamond standard (Linux)"),
        ("pyepics", "epics", "recommended on Windows"),
        ("aa.ca", "aa.ca", "archiver package CA module"),
    ]:
        try:
            __import__(mod)
            print(f"{OK}  {name:10s} available   ({note})")
            found.append(name)
        except Exception as exc:
            reason = str(exc).split("\n")[0][:52]
            print(f"{'    -':5s}  {name:10s} not installed  ({reason})")
    if not found:
        print(f"\n{BAD}  No live backend. Install one:")
        print("        pip install pyepics       # Windows and Linux")
        print("        pip install cothread      # Linux only (needs a C compiler)")
    return found


def check_pvs(enc, label, watch=False):
    banner(f"2. Encoder PVs — {label}")
    if not enc.pv_map:
        print(f"{WARN}  no PVs configured")
        return
    width = max(len(v) for v in enc.pv_map.values())
    rounds = 20 if watch else 1
    for i in range(rounds):
        readings = enc.read_all()
        if watch and i:
            print(f"\r\033[{len(readings)+1}A", end="")
        print(f"{'encoder':10s} {'PV':{width}s} {'value':>12s}  {'age':>7s}  state")
        for eid, r in readings.items():
            pv = enc.pv_map[eid]
            if r["value"] is None:
                print(f"{eid:10s} {pv:{width}s} {'—':>12s}  {'—':>7s}  "
                      f"NOT CONNECTED  {r.get('error') or ''}")
            else:
                age = r["age"]
                state = "stale" if r["stale"] else "live"
                print(f"{eid:10s} {pv:{width}s} {r['value']:12.4f}  "
                      f"{age:6.1f}s  {state}")
        if watch:
            time.sleep(0.5)
    bad = [e for e, r in readings.items() if r["value"] is None]
    if bad:
        print(f"\n{BAD}  {len(bad)} PV(s) not connected: {', '.join(bad)}")
        print("        Check the IOC is running and that EPICS_CA_ADDR_LIST reaches it.")
        return False
    print(f"\n{OK}  all {len(readings)} encoder PVs connected")
    return True


def check_temperature(temp):
    banner("3. Bay temperature sensors")
    if not temp.bay_ids:
        print(f"{WARN}  no bays configured")
        return
    for bay in temp.bay_ids:
        r = temp.read_bay(bay)
        if r["temperature"] is None:
            print(f"{BAD}  bay {bay}: no data — {r.get('error')}")
            continue
        mark = OK if r["health"] == "ok" else WARN
        print(f"{mark}  bay {bay}: {r['temperature']:.2f} °C   "
              f"{r['sensors_used']}/{r['sensors_total']} sensors   "
              f"spread {r['spread']:.2f} °C   health={r['health']}")
        if r["failed"]:
            print(f"        not reporting: {', '.join(r['failed'])}")


def check_archiver(cfg):
    banner("4. Archiver Appliance")
    ep = cfg.get("epics", {})
    host, port = ep.get("archiver_host"), int(ep.get("archiver_port", 80))
    print(f"        JSON endpoint : {host}:{port}")
    if ep.get("archiver_rest_host"):
        print(f"        REST endpoint : {ep['archiver_rest_host']}:"
              f"{ep.get('archiver_rest_port', 80)}")
    arch = epics_io.Archiver(host, port)
    if not arch.available:
        print(f"{WARN}  {arch.error}")
        if "protobuf" in (arch.error or ""):
            print('        pip install "protobuf<3.21"')
        else:
            print("        pip install aapy   (installs as aapy, imports as aa)")
        print("        The archiver is optional — it only adds move traces to reports.")
        return False
    pv = next(iter(ep.get("pv_map", {}).values()), None)
    if not pv:
        print(f"{WARN}  no PV to test with")
        return False
    end = datetime.now(timezone.utc)
    pts = arch.trace(pv, end - timedelta(minutes=10), end)
    if pts:
        print(f"{OK}  retrieved {len(pts)} sample(s) for {pv} over 10 min")
        print(f"        latest: {pts[-1][1]:.4f} at "
              f"{datetime.fromtimestamp(pts[-1][0], timezone.utc):%H:%M:%S} UTC")
        return True
    print(f"{WARN}  no samples for {pv} — {arch.error or 'PV may not be archived'}")
    return False


def main():
    ap = argparse.ArgumentParser(description="EPICS connectivity check")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--bay", help="only this bay")
    ap.add_argument("--backend", default=None,
                    help="force a backend: cothread | pyepics | aa.ca | demo")
    ap.add_argument("--watch", action="store_true",
                    help="keep polling the encoders (Ctrl-C to stop)")
    ap.add_argument("--find", metavar="PATTERN",
                    help="ask the archiver which PVs match, e.g. 'TS01C-AL-GIRDR-01*'. "
                         "Works even when Channel Access is blocked.")
    ap.add_argument("--probe", nargs="*", metavar="PV",
                    help="report which IOC serves these PVs. Pass a PV you KNOW "
                         "works (a live machine one) alongside a failing one to "
                         "compare the serving hosts.")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    ep = cfg.get("epics", {})

    print("=" * 60)
    print("  Girder alignment — EPICS connectivity check")
    print(f"  config: {args.config}")
    print("=" * 60)

    if args.find:
        find_pvs(cfg, args.find)
        return 0

    found = check_backends()
    check_ca_environment()
    if args.probe is not None:
        pvs = args.probe or list(ep.get("pv_map", {}).values())[:2]
        probe_hosts(pvs)

    name = args.backend or ep.get("live_backend", "auto")
    try:
        backend = (epics_io.DemoBackend() if name == "demo"
                   else epics_io.make_live_backend(name))
    except Exception as exc:
        print(f"\n{BAD}  {exc}")
        return 1
    print(f"\n        using backend: {backend.name}")
    if backend.name == "demo":
        print(f"{WARN}  demo backend — these are simulated values, not real PVs")

    ca_failed = False
    bays = ep.get("bay_pv_map") or {}
    targets = [args.bay] if args.bay else (sorted(bays) or [None])
    for bay in targets:
        enc = epics_io.EncoderService(ep.get("pv_map", {}), backend,
                                      ep.get("scale") or {}, bays)
        if bay:
            enc.set_bay(bay)
        try:
            if not check_pvs(enc, f"bay {bay}" if bay else "default PV set",
                             args.watch):
                ca_failed = True
        except KeyboardInterrupt:
            print("\nstopped")

    tc = cfg.get("temperature", {}) or {}
    check_temperature(epics_io.TemperatureService(
        tc.get("sensor_pvs", {}), tc.get("bays", {}), backend))

    arch_ok = check_archiver(cfg)

    banner("5. Summary")
    if backend.name == "demo":
        print(f"{WARN}  running on simulated data")
    elif ca_failed and arch_ok:
        print(f"{BAD}  Channel Access cannot reach the IOCs, but the archiver CAN "
              "be read.")
        print("        That usually means this PC is outside the controls network.")
        print("        Options, best first:")
        print("          1. Get CA access to the IOC subnet (set EPICS_CA_ADDR_LIST,")
        print("             and have the firewall allow UDP/TCP 5064-5065).")
        print("          2. Run the tool on a PC that already has CA access.")
        print("          3. Temporary fallback — set live_backend: archiver in")
        print("             config.yaml. Values then come from the archiver, which is")
        print("             deadbanded and seconds late. Adequate for looking around,")
        print("             NOT for following a move.")
    elif ca_failed:
        print(f"{BAD}  Neither Channel Access nor the archiver responded.")
    else:
        print(f"{OK}  live EPICS and archiver both reachable")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
