#!/usr/bin/env python3
"""S5a F5 — backup-before-live: snapshot every {master,dups} pair + confirm PROV blobs, then a dated
read-only export BACKSTOP. Read-only against the library (GET only); the only writes are new PROV
snapshot records (the sanctioned append-only audit) and the local dated export file. NEVER a Zotero
mutation.

The PRIMARY restore path stays rollback_merge + trash recovery (merge-safety.md) — this export is a
belt-and-suspenders backstop, not the rollback mechanism.

Usage:
    python scripts/backup_before_live.py --pairs pairs.json
        pairs.json: [{"master": "KEY1", "dups": ["KEY2","KEY3"]}, ...]

    python scripts/backup_before_live.py --from-dedup-scan
        Backs up every current auto-accept dedup cluster over the live library (the natural
        pre-S2-style-run backstop) instead of a hand-authored pairs file.

    python scripts/backup_before_live.py --pairs pairs.json --no-export
        Snapshot + blob-confirm only; skip the dated JSON export.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from zotero_write_mcp.backup import backup_before_live  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402
from zotero_write_mcp.dedup import dedup_scan  # noqa: E402
from zotero_write_mcp.merge_live import WebClusterReader  # noqa: E402
from zotero_write_mcp.webscan import web_items  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "exit-gate-runs", "backups")


def _data(it):
    return it.get("data", it)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", help="path to a JSON file of [{master, dups}, ...]")
    ap.add_argument("--from-dedup-scan", action="store_true",
                    help="back up every current auto-accept dedup cluster instead of a pairs file")
    ap.add_argument("--no-export", action="store_true", help="skip the dated JSON export (snapshot-only)")
    args = ap.parse_args()
    if not args.pairs and not args.from_dedup_scan:
        print("FAIL: pass --pairs <file.json> or --from-dedup-scan.")
        return 1

    client = ZoteroClient()
    reader = WebClusterReader(client, client.library_id)
    prov = client.prov

    if args.from_dedup_scan:
        print(f"[SCAN] paging library {client.library_id} + dedup_scan ...", flush=True)
        items = web_items(client)
        rep = dedup_scan(items)
        auto = [c for c in rep["candidate_clusters"] if c.auto_accept]
        pairs = [{"master": c.master_key, "dups": [k for k in c.item_keys if k != c.master_key]}
                 for c in auto]
        print(f"[SCAN] {len(items)} items; {len(pairs)} auto-accept clusters to back up", flush=True)
    else:
        pairs = json.load(open(args.pairs, encoding="utf-8"))
        print(f"[LOAD] {len(pairs)} pairs from {args.pairs}", flush=True)

    def _export_fn(keys):
        out = []
        for k in keys:
            try:
                out.append({"key": k, "data": _data(reader.get_item(k))})
            except Exception as e:
                out.append({"key": k, "error": f"{type(e).__name__}: {e}"})
        return {"items": out}

    export_fn = None if args.no_export else _export_fn
    res = backup_before_live(pairs, reader, prov, export_fn=export_fn, export_dir=OUT_DIR)

    print("\n" + "=" * 78)
    print(f"BACKUP-BEFORE-LIVE  ·  {res.pairs_backed_up} pairs")
    print(f"  snapshot blobs confirmed : {len(res.blob_confirmed)}/{len(res.snapshot_ids)}")
    if res.blob_missing:
        print(f"  BLOB MISSING (ALERT)    : {res.blob_missing}")
    if res.export_path:
        print(f"  export written           : {res.export_path} ({res.export_item_count} items)")
    print("=" * 78)

    return 0 if not res.blob_missing else 2


if __name__ == "__main__":
    sys.exit(main())
