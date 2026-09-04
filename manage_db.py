#!/usr/bin/env python3
"""
Session database maintenance.

Short answer to "can I just delete the db file?": yes, it is a single SQLite file
and deleting it is safe — the server recreates an empty one on next start. But it
throws away the audit trail for every girder ever aligned, and any report you have
not already generated becomes unrecoverable, so it is worth knowing the narrower
options first.

    python manage_db.py list                    # what is in there
    python manage_db.py stats                   # size and counts
    python manage_db.py archive                 # timestamped copy, then keep going
    python manage_db.py delete <id> [<id> ...]  # remove specific sessions
    python manage_db.py purge --before 2026-01-01
    python manage_db.py purge --test            # drop obvious test/demo rows
    python manage_db.py clear                   # empty it (archives first)
    python manage_db.py vacuum                  # reclaim space after deleting

Everything destructive archives the database first unless you pass --no-archive,
and asks for confirmation unless you pass --yes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_DB = HERE / "sessions" / "sessions.sqlite"


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path} (nothing to do)")
    return sqlite3.connect(str(path))


def archive(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.stem}_{stamp}.bak{path.suffix}")
    shutil.copy2(path, dest)
    print(f"archived -> {dest}")
    return dest


def confirm(msg: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")


def cmd_list(args):
    db = _connect(args.db)
    rows = db.execute(
        "SELECT id,serial,gtype,operator,created,updated,status "
        "FROM sessions ORDER BY updated DESC").fetchall()
    if not rows:
        print("(empty)")
        return
    print(f"{'id':14s} {'serial':14s} {'type':5s} {'operator':16s} "
          f"{'status':9s} updated")
    for r in rows:
        print(f"{r[0]:14s} {(r[1] or ''):14s} {(r[2] or ''):5s} "
              f"{(r[3] or '')[:16]:16s} {(r[6] or ''):9s} {(r[5] or '')[:19]}")
    print(f"\n{len(rows)} session(s)")


def cmd_stats(args):
    db = _connect(args.db)
    n = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    by_status = db.execute(
        "SELECT status,COUNT(*) FROM sessions GROUP BY status").fetchall()
    by_type = db.execute(
        "SELECT gtype,COUNT(*) FROM sessions GROUP BY gtype").fetchall()
    size = args.db.stat().st_size / 1024.0
    print(f"database : {args.db}")
    print(f"size     : {size:,.1f} kB")
    print(f"sessions : {n}")
    print(f"by status: {dict(by_status)}")
    print(f"by type  : {dict(by_type)}")


def cmd_delete(args):
    db = _connect(args.db)
    found = [r[0] for r in db.execute(
        "SELECT id FROM sessions WHERE id IN (%s)" %
        ",".join("?" * len(args.ids)), args.ids).fetchall()]
    missing = [i for i in args.ids if i not in found]
    if missing:
        print(f"not found: {', '.join(missing)}")
    if not found:
        return
    if not confirm(f"delete {len(found)} session(s)?", args.yes):
        return print("cancelled")
    if not args.no_archive:
        archive(args.db)
    db.execute("DELETE FROM sessions WHERE id IN (%s)" %
               ",".join("?" * len(found)), found)
    db.commit()
    print(f"deleted {len(found)} session(s). Run 'vacuum' to reclaim space.")


def cmd_purge(args):
    db = _connect(args.db)
    if args.test:
        # test rows are the ones whose serial is not a real DLS part number
        rows = db.execute("SELECT id,serial FROM sessions").fetchall()
        ids = [r[0] for r in rows
               if not str(r[1] or "").upper().startswith("DLS")]
        what = "test/demo sessions (serial not starting DLS)"
    elif args.before:
        rows = db.execute("SELECT id FROM sessions WHERE created < ?",
                          (args.before,)).fetchall()
        ids = [r[0] for r in rows]
        what = f"sessions created before {args.before}"
    else:
        return print("give --before YYYY-MM-DD or --test")
    if not ids:
        return print(f"nothing matches {what}")
    if not confirm(f"delete {len(ids)} {what}?", args.yes):
        return print("cancelled")
    if not args.no_archive:
        archive(args.db)
    db.execute("DELETE FROM sessions WHERE id IN (%s)" %
               ",".join("?" * len(ids)), ids)
    db.commit()
    print(f"deleted {len(ids)} session(s). Run 'vacuum' to reclaim space.")


def cmd_clear(args):
    db = _connect(args.db)
    n = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if not confirm(f"REMOVE ALL {n} session(s)? "
                   "Reports already written to disk are not affected.", args.yes):
        return print("cancelled")
    if not args.no_archive:
        archive(args.db)
    db.execute("DELETE FROM sessions")
    db.commit()
    db.execute("VACUUM")
    print(f"cleared {n} session(s)")


def cmd_vacuum(args):
    db = _connect(args.db)
    before = args.db.stat().st_size
    db.execute("VACUUM")
    db.commit()
    after = args.db.stat().st_size
    print(f"{before/1024:,.1f} kB -> {after/1024:,.1f} kB")


def cmd_export(args):
    """Dump every session to JSON before you throw the database away."""
    db = _connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = db.execute("SELECT id,serial,gtype,blob FROM sessions").fetchall()
    for sid, serial, gtype, blob in rows:
        (out / f"session_{serial or 'unknown'}_{gtype or ''}_{sid}.json").write_text(blob)
    print(f"exported {len(rows)} session(s) -> {out}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    ap.add_argument("--no-archive", action="store_true",
                    help="do not take a backup copy first")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        # accept --yes / --no-archive either before or after the subcommand
        sp.add_argument("--yes", action="store_true", help="skip confirmation")
        sp.add_argument("--no-archive", action="store_true",
                        help="do not take a backup copy first")
        return sp

    common(sub.add_parser("list")).set_defaults(func=cmd_list)
    common(sub.add_parser("stats")).set_defaults(func=cmd_stats)
    common(sub.add_parser("vacuum")).set_defaults(func=cmd_vacuum)
    common(sub.add_parser("clear")).set_defaults(func=cmd_clear)

    a = common(sub.add_parser("archive"))
    a.set_defaults(func=lambda args: archive(args.db))

    d = common(sub.add_parser("delete"))
    d.add_argument("ids", nargs="+")
    d.set_defaults(func=cmd_delete)

    p = common(sub.add_parser("purge"))
    p.add_argument("--before", help="YYYY-MM-DD")
    p.add_argument("--test", action="store_true",
                   help="drop sessions whose serial is not a DLS number")
    p.set_defaults(func=cmd_purge)

    e = common(sub.add_parser("export"))
    e.add_argument("--out", default=str(HERE / "sessions" / "export"))
    e.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
