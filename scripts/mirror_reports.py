#!/usr/bin/env python3
"""Copy alignment reports off a deployment onto backed-up storage.

WHY THIS EXISTS. The deployed service writes reports to a PersistentVolumeClaim
(``/data/reports`` in the pod). A PVC is working storage: it is not backed up,
it has no snapshots unless the storage class provides them, and it is one
``kubectl delete pvc`` or one cluster migration away from gone. Alignment
reports are the record of work done on real hardware, so they need to live
somewhere that is actually backed up.

The obvious answer - mount the group share into the pod and write there
directly - does not work at DLS: the accelerator IOC nodes mount ``/dls_sw``
but not ``/dls``, so there is no host path to bind. Rather than put an NFS
volume and a network dependency into the pod, this pulls from the outside,
using the same HTTP endpoints the web UI uses. Run it from any machine that can
reach the service and has the share mounted.

    mirror_reports.py --url https://deb-girder-bay-01.diamond.ac.uk \
                      --dest /dls/sdrive/<group>/girder-alignment/bay1 --sessions

and from cron, as often as you like:

    */15 * * * * /path/to/mirror_reports.py --url ... --dest ... >>mirror.log 2>&1

``--sessions`` additionally saves the whole session store as
``sessions_<YYYYmmdd>.json``. Every report already carries its own session, so
this only covers the ones that never produced a report - abandoned partway, or
open when the volume is lost. It is fetched as JSON rather than by copying
``sessions.sqlite``, because a file copy taken while the service is writing can
be torn and may not open.

SAFETY. This only ever adds. It never deletes, never overwrites a report, and
downloads to ``<name>.part`` before renaming into place, so an interrupted run
cannot leave a truncated PDF that later looks complete. Repeated runs are safe,
but overlapping runs must be prevented by the scheduler: they share the same
temporary filenames. The one file it replaces is the current day's session
snapshot, which is the point of a snapshot.

Standard library only, so it needs no virtualenv on the machine that runs it.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

#: Reports are written as girder_<serial>_<type>_<stamp>_<FINAL|PARTIAL>.pdf
#: with a .json sibling holding the full session. /api/reports lists only the
#: PDFs, so the sibling is derived from the name and fetched best-effort - see
#: fetch_one().
JSON_SUFFIX = ".json"


def fetch(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read()


def listing(base: str, timeout: float) -> list[str]:
    """The PDFs the service is currently holding, newest first."""
    raw = json.loads(fetch(f"{base}/api/reports", timeout))
    if not raw.get("ok"):
        raise RuntimeError(f"{base}/api/reports returned {raw!r}")
    return list(raw.get("reports") or [])


def fetch_one(base: str, name: str, dest: Path, timeout: float, dry_run: bool) -> bool:
    """Copy one file across if it is not there already. True if it was new.

    A missing ``.json`` sibling is not an error. The PDF and the JSON each take
    their own timestamp when they are written, so a report generated across a
    second boundary has stems one second apart and the sibling cannot be
    derived. The PDF is the record; the JSON is a convenience.
    """
    out = dest / name
    if out.exists():
        return False
    if dry_run:
        print(f"would fetch {name}")
        return True
    url = f"{base}/reports/{urllib.parse.quote(name)}"
    try:
        data = fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        if name.endswith(JSON_SUFFIX) and exc.code == 404:
            return False
        raise
    # Download to one side and rename, so a killed run never leaves a truncated
    # file that the next run would skip as "already there".
    part = out.with_name(out.name + ".part")
    part.write_bytes(data)
    part.rename(out)
    print(f"fetched {name} ({len(data)} bytes)")
    return True


def fetch_sessions(base: str, dest: Path, timeout: float, dry_run: bool) -> bool:
    """Save the whole session store as sessions_<YYYYmmdd>.json. True if written.

    Dated, so the snapshots accumulate rather than one file being rewritten
    forever - a session deleted or corrupted today is still in yesterday's.
    Within a day the file is replaced, which is what a daily snapshot means.
    """
    name = f"sessions_{datetime.now(UTC).strftime('%Y%m%d')}.json"
    out = dest / name
    if dry_run:
        print(f"would fetch {name}")
        return True
    raw = json.loads(fetch(f"{base}/api/sessions/export", timeout))
    if not raw.get("ok"):
        raise RuntimeError(f"{base}/api/sessions/export returned {raw!r}")
    part = out.with_name(out.name + ".part")
    part.write_text(json.dumps(raw, indent=1))
    # replace, not rename: today's snapshot is meant to be superseded.
    part.replace(out)
    print(f"fetched {name} ({raw.get('count', 0)} sessions)")
    return True


def mirror(
    base: str, dest: Path, timeout: float, dry_run: bool, sessions: bool = False
) -> int:
    base = base.rstrip("/")
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for pdf in listing(base, timeout):
        for name in (pdf, pdf.removesuffix(".pdf") + JSON_SUFFIX):
            count += fetch_one(base, name, dest, timeout, dry_run)
    if sessions:
        count += fetch_sessions(base, dest, timeout, dry_run)
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        required=True,
        help="base URL of the deployment, e.g. https://deb-girder-bay-01.diamond.ac.uk",
    )
    parser.add_argument(
        "--dest",
        required=True,
        type=Path,
        help="directory to mirror into. One per bay - the filenames do not "
        "carry the bay, so two deployments must not share a destination.",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="per-request timeout in seconds"
    )
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="also save the whole session store as sessions_<YYYYmmdd>.json. "
        "Covers sessions that never produced a report; the rest are already "
        "in the .json beside each PDF.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be copied, copy nothing"
    )
    args = parser.parse_args(argv)

    try:
        count = mirror(args.url, args.dest, args.timeout, args.dry_run, args.sessions)
    except (OSError, RuntimeError, ValueError) as exc:
        # Non-zero so cron mails somebody. A share that has gone away and a
        # service that is down both land here, and both want a human.
        print(f"mirror failed: {exc}", file=sys.stderr)
        return 1
    print(f"{count} new file(s) -> {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
