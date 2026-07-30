#!/usr/bin/env python
"""Owner-invoked resolution for PROV log damage (Routine Supervised v1.0, LOG-001/REC-006).

When ``scan_integrity`` reports ``status=blocked``, every destructive engine operation refuses
until the owner inspects each malformed line and explicitly accepts it. This script is that
resolution path. It is deliberately a SCRIPT rather than an MCP tool: acceptance is an owner act
performed out-of-band from the agent workflow.

Usage:
  python scripts/accept_prov_damage.py                 # show the integrity report (read-only)
  python scripts/accept_prov_damage.py --accept LINE_NO SHA256 --note "why this is accepted"

The damaged line is never rewritten or deleted (append-only store). Acceptance appends a
``log_damage_accepted`` record naming the exact (line_no, sha256); the line remains permanently
visible in every future integrity report under ``accepted``.

This is NOT a cryptographic control (LOG-003): in the supervised single-principal deployment the
acceptance record is same-principal-writable. It provides visibility and an audit trail.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zotero_write_mcp.provenance import ProvenanceStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prov-dir", default=os.environ.get("ZOTERO_PROV_DIR", "runtime/prov"))
    ap.add_argument("--accept", nargs=2, metavar=("LINE_NO", "SHA256"),
                    help="accept exactly one damaged line by its line number and sha256")
    ap.add_argument("--note", default=None, help="required human note explaining the acceptance")
    ap.add_argument("--operator", default="owner")
    args = ap.parse_args()

    store = ProvenanceStore(args.prov_dir)
    report = store.scan_integrity()

    if not args.accept:
        print(json.dumps(report, indent=2))
        return 0 if report["status"] != "blocked" else 2

    if not args.note:
        ap.error("--accept requires --note explaining why the damage is accepted")
    line_no, sha = int(args.accept[0]), args.accept[1]
    # F-2: match against BOTH detail views — `unaccepted` prioritises still-blocking lines, so when
    # damage exceeds the detail cap, successive accept-and-rescan rounds surface the remainder.
    candidates = {(b["line_no"], b["line_sha256"]) for b in report["bad_lines"]} | \
                 {(b["line_no"], b["line_sha256"]) for b in report["unaccepted"]}
    if (line_no, sha) not in candidates:
        print(f"REFUSED: no damaged line at line_no={line_no} with sha256={sha}. "
              "Run without --accept to list the current damage.", file=sys.stderr)
        return 2
    rec = store.accept_log_damage(line_no, sha, note=args.note, operator=args.operator)
    after = store.scan_integrity()
    print(json.dumps({"accepted": rec["params"], "prov_id": rec["prov_id"],
                      "status_after": after["status"],
                      "unaccepted_remaining": len(after["unaccepted"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
