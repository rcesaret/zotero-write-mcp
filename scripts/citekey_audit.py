#!/usr/bin/env python3
"""S5a F7 — citekey-collision + tex.ids alias-survival sweep, over the WHOLE live library. Read-only:
pages the library via the Web API (never the local API — unaffected by local-API host-health
degradation) and, for the alias check, does one extra GET per dc:replaces pair (currently ~151 from
the S2 mass merge) to read each trashed secondary's own pinned citekey.

Usage:
    python scripts/citekey_audit.py                  # collisions + alias check
    python scripts/citekey_audit.py --no-aliases      # collision-only (fast; single library pass)
    python scripts/citekey_audit.py --out report.json

Requires ZOTERO_API_KEY (write-gateway env; only used here for a read-only client).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from zotero_write_mcp.citekeys import scan_citekey_collisions, scan_tex_ids_aliases  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402
from zotero_write_mcp.webscan import web_items  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-aliases", action="store_true", help="skip the tex.ids alias-survival check")
    ap.add_argument("--out", help="write the full report JSON to this path (in addition to stdout summary)")
    args = ap.parse_args()

    client = ZoteroClient()
    print(f"[SCAN] paging library {client.library_id} via the Web API ...", flush=True)
    items = web_items(client)
    print(f"[SCAN] {len(items)} live items", flush=True)

    collisions = scan_citekey_collisions(items)
    aliases = None
    if not args.no_aliases:
        def _lookup(key: str):
            try:
                return client.get_item_web(key)
            except Exception:
                return None
        aliases = scan_tex_ids_aliases(items, _lookup)

    report = {"collisions": collisions, "tex_ids_aliases": aliases}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, ensure_ascii=False)
        print(f"[OUT] full report written to {args.out}")

    print("\n" + "=" * 78)
    print(f"CITEKEY AUDIT  ·  {collisions['items_scanned']} items scanned")
    print(f"  unique citekeys      : {collisions['unique_citekeys']}")
    print(f"  keyless items        : {collisions['keyless']}")
    print(f"  COLLISIONS           : {collisions['collision_count']}"
         f"{'  ' + str(list(collisions['collisions'])[:5]) if collisions['collision_count'] else ''}")
    if aliases is not None:
        print(f"  dc:replaces pairs checked : {aliases['dc_replaces_pairs_checked']}")
        print(f"  MISSING ALIASES           : {aliases['missing_alias_count']}")
        if aliases["missing_alias_count"]:
            print(f"    {aliases['missing_alias'][:5]}")
    print("=" * 78)

    gate = collisions["collision_count"] == 0 and (aliases is None or aliases["missing_alias_count"] == 0)
    return 0 if gate else 2


if __name__ == "__main__":
    sys.exit(main())
