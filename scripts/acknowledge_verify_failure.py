#!/usr/bin/env python3
"""OBS-5 operator script — acknowledge ONE human-reviewed verify failure by snapshot_id.

Appends a `verify_failure_acknowledged` PROV record so `daily_report` stops counting that single,
reviewed failure against the C-2 health rate. Writes ONLY to the local PROV store — no Zotero write.
Fail-closed: refuses unless a FAILED verify record with the given snapshot_id exists, requires a
non-empty --reason, and refuses a double-acknowledgment. The failed record itself is never altered.

Run by the OWNER (or with the owner's explicit per-record approval) after an incident is investigated:

    uv run python scripts/acknowledge_verify_failure.py \
        --snapshot-id <id> --reason "root cause + fix reference" [--by owner]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zotero_write_mcp.observability import acknowledge_verify_failure, daily_report  # noqa: E402
from zotero_write_mcp.provenance import ProvenanceStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Acknowledge one reviewed verify failure (OBS-5).")
    ap.add_argument("--snapshot-id", required=True, help="snapshot_id of the FAILED verify record")
    ap.add_argument("--reason", required=True, help="root cause + fix reference (audit record)")
    ap.add_argument("--by", default="owner", help="who reviewed/acknowledged (default: owner)")
    args = ap.parse_args()

    root = os.environ.get("ZOTERO_PROV_DIR", str(Path.home() / ".zotero-write-mcp" / "prov"))
    prov = ProvenanceStore(root)
    try:
        rec = acknowledge_verify_failure(prov, args.snapshot_id, reason=args.reason,
                                         acknowledged_by=args.by)
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 1
    print(f"ACKNOWLEDGED snapshot_id={args.snapshot_id} by={args.by}")
    print(f"  failed_checks: {rec.get('params', {}).get('failed_checks')}")
    rep = daily_report(prov)
    print(f"  fresh daily_report: status={rep['status']} rate={rep['verify_pass_rate']} "
          f"acknowledged={rep['verify_failed_acknowledged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
