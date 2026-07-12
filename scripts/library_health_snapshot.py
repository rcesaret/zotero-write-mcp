#!/usr/bin/env python3
"""S5a — library-health snapshot: computes the six NFR-OBSERVABLE metrics in ONE library pass and
writes a dated JSON (never overwrites a prior snapshot). This is a RENDERER's data source, not a new
collector — every number is a thin aggregation over already-built PROV / dedup / citekey code
(PLAN3 §0/§6.3). Read-only: pages the library via the Web API (never the local API), reads the PROV
store, and reads the local storage directory + the harness's control-plane audit log. Makes NO Zotero
write.

Data-honesty rule (non-negotiable, S5a §C.1): every metric is either computed from a named source or
explicitly labeled "not measured / deferred". No estimated, LLM-guessed, or placeholder numbers.

Usage:
    python scripts/library_health_snapshot.py
    python scripts/library_health_snapshot.py --out-dir exit-gate-runs/dashboard
    python scripts/library_health_snapshot.py --harness-root ../zotero_harness
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from zotero_write_mcp import citekeys as CK  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402
from zotero_write_mcp.dedup import dedup_scan  # noqa: E402
from zotero_write_mcp.webscan import web_items  # noqa: E402

OUT_DIR_DEFAULT = os.path.join(os.path.dirname(__file__), "..", "exit-gate-runs", "dashboard")

# Item types that carry a DOI field in Zotero's schema. Excludes types that structurally never have
# one (note, webpage, letter, ...) so the % is meaningful rather than diluted by non-DOI-bearing types.
DOI_BEARING_TYPES = {
    "journalArticle", "conferencePaper", "preprint", "thesis", "report",
    "bookSection", "book", "dataset", "manuscript",
}


def _data(it):
    return it.get("data", it)


def metric_duplicate_clusters(items: list) -> dict:
    rep = dedup_scan(items)
    return {
        "source": "dedup_scan(items) over the live (non-trashed, non-attachment) library",
        "candidate_clusters": len(rep["candidate_clusters"]),
        "auto_accept_clusters": rep["auto_accept_count"],
        "review_queue_clusters": rep["review_count"],
    }


def metric_missing_doi(items: list) -> dict:
    eligible = [it for it in items if _data(it).get("itemType") in DOI_BEARING_TYPES]
    missing = [it for it in eligible if not (_data(it).get("DOI") or "").strip()]
    pct = (len(missing) / len(eligible) * 100.0) if eligible else None
    return {
        "source": f"live items whose itemType is in {sorted(DOI_BEARING_TYPES)} (DOI-bearing types only)",
        "eligible_items": len(eligible),
        "missing_doi": len(missing),
        "pct_missing": round(pct, 2) if pct is not None else None,
    }


def metric_low_density_attachments() -> dict:
    return {
        "source": None,
        "status": "not measured — Phase-5 PDF hygiene (scan_pdf_quality) is DEFERRED to v1.2 "
                 "(v1-plan.md decision; not built by S5a)",
        "value": None,
    }


def metric_orphan_files(client, storage_dir: str) -> dict:
    storage = Path(storage_dir)
    if not storage.is_dir():
        return {"source": storage_dir, "status": f"storage dir not found at {storage_dir}", "value": None}
    on_disk = {p.name for p in storage.iterdir() if p.is_dir()}
    # include_trashed=True: a trashed-but-not-purged attachment still legitimately owns its file.
    attachments = web_items(client, item_type="attachment", include_trashed=True)
    known_keys = {(_data(a).get("key") or a.get("key")) for a in attachments}
    orphans = sorted(on_disk - known_keys)
    return {
        "source": f"storage-dir folders under {storage_dir} vs. ALL (incl. trashed) attachment item "
                 "keys from the Web API",
        "storage_folders": len(on_disk),
        "known_attachment_keys": len(known_keys),
        "orphan_count": len(orphans),
        "orphan_sample": orphans[:10],
    }


def metric_citekey_collisions(items: list) -> dict:
    rep = CK.scan_citekey_collisions(items)
    rep["source"] = "citekeys.scan_citekey_collisions over the live library (extra Citation Key: / citationKey)"
    rep["note"] = ("tex.ids alias-survival check NOT run here for speed — see "
                   "scripts/citekey_audit.py for the full sweep")
    return rep


def metric_prov_coverage(prov, harness_audit_path: str) -> dict:
    engine_total = prov.count()
    control_plane_lines = 0
    control_plane_path_exists = os.path.isfile(harness_audit_path)
    if control_plane_path_exists:
        with open(harness_audit_path, encoding="utf-8") as f:
            control_plane_lines = sum(1 for line in f if line.strip())
    ratio = (min(engine_total, control_plane_lines) / control_plane_lines
            if control_plane_lines else None)
    return {
        "source": "ProvenanceStore.count() (engine, authoritative) vs. the harness's "
                 "control-plane-audit.jsonl (PostToolUse backstop; per its own docstring the engine "
                 "record is authoritative)",
        "engine_prov_records": engine_total,
        "control_plane_audit_lines": control_plane_lines if control_plane_path_exists else None,
        "control_plane_audit_found": control_plane_path_exists,
        "approx_coverage_ratio": round(ratio, 3) if ratio is not None else None,
        "caveat": ("APPROXIMATE ONLY — the two logs use different activity-name granularity "
                  "(e.g. control-plane logs the MCP tool name; the engine logs its internal "
                  "activity) and the control-plane entries carry no timestamp, so an exact "
                  "per-mutation cross-reference is not currently computable. This is a raw "
                  "record-count sanity check, not a verified 1:1 audit."),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    ap.add_argument("--storage-dir", default=str(Path.home() / "Zotero" / "storage"))
    ap.add_argument("--harness-root", default=os.path.join(os.path.dirname(__file__), "..", "..", "zotero_harness"))
    args = ap.parse_args()

    client = ZoteroClient()
    now = datetime.now(timezone.utc)

    print(f"[SCAN] paging library {client.library_id} (non-attachment items) ...", flush=True)
    items = web_items(client)
    print(f"[SCAN] {len(items)} items", flush=True)

    harness_audit = os.path.join(args.harness_root, "runtime", "prov", "control-plane-audit.jsonl")

    metrics = {
        "duplicate_clusters": metric_duplicate_clusters(items),
        "missing_doi": metric_missing_doi(items),
        "low_density_attachments": metric_low_density_attachments(),
        "orphan_files": metric_orphan_files(client, args.storage_dir),
        "citekey_collisions": metric_citekey_collisions(items),
        "prov_coverage": metric_prov_coverage(client.prov, harness_audit),
    }

    snapshot = {
        "ts": now.isoformat(),
        "library_id": client.library_id,
        "items_scanned": len(items),
        "metrics": metrics,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"health-{stamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=1, ensure_ascii=False)

    print("\n" + "=" * 78)
    print(f"LIBRARY HEALTH SNAPSHOT  ·  {len(items)} items  ·  {now.isoformat()}")
    print(f"  duplicate clusters   : {metrics['duplicate_clusters']['candidate_clusters']} candidate "
         f"({metrics['duplicate_clusters']['auto_accept_clusters']} auto-accept)")
    m = metrics["missing_doi"]
    print(f"  % missing DOI        : {m['pct_missing']}%  ({m['missing_doi']}/{m['eligible_items']} eligible)")
    print(f"  low-density attach.  : {metrics['low_density_attachments']['status']}")
    o = metrics["orphan_files"]
    print(f"  orphan files         : {o.get('orphan_count', 'n/a')}")
    c = metrics["citekey_collisions"]
    print(f"  citekey collisions   : {c['collision_count']}")
    p = metrics["prov_coverage"]
    print(f"  PROV coverage (approx): {p['approx_coverage_ratio']}  "
         f"(engine={p['engine_prov_records']}, control-plane={p['control_plane_audit_lines']})")
    print(f"  snapshot written     : {out_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
