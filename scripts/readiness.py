#!/usr/bin/env python3
"""S5a F1 — pre-live readiness report, printed as JSON. Read-only: no Zotero writes, no live-gate
env vars set. Wraps `zotero_write_mcp.readiness.readiness_report` so the harness's `zot-doctor`
scripts (`scripts/doctor.sh` / `.ps1`) can call ONE command and format its rows.

Usage:
    python scripts/readiness.py                    # full report incl. the local-API latency probe
    python scripts/readiness.py --no-local-api      # skip the local-API probe (e.g. no Zotero running)
    python scripts/readiness.py --recent-n 5

Exit code: 0 always (this is a report, not a gate) — callers read the JSON's status fields.
Requires ZOTERO_API_KEY (the PROV store root and engine version checks do not need live Zotero
reachability; only --local-api, the default, does).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from zotero_write_mcp.provenance import ProvenanceStore  # noqa: E402
from zotero_write_mcp.readiness import readiness_report  # noqa: E402


def _prov_root() -> str:
    from pathlib import Path
    return os.environ.get("ZOTERO_PROV_DIR") or str(Path.home() / ".zotero-write-mcp" / "prov")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-local-api", action="store_true",
                    help="skip the local-API latency probe (row 5 / live_create_safe_now)")
    ap.add_argument("--recent-n", type=int, default=20, help="unused here; kept for symmetry with prov_coverage_report")
    args = ap.parse_args()

    prov = ProvenanceStore(_prov_root())
    report = readiness_report(prov, probe_local_api=not args.no_local_api)
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
