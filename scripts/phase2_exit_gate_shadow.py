#!/usr/bin/env python3
"""Phase-2 ZERO-WRITE exit-gate shadow run over REAL deterministic auto-accept duplicate clusters.

PRD §12 exit gate: a shadow run over a real subset producing (a) 100% verify-pass and (b) a sampled
human audit showing zero false merges, with query_provenance + a daily verify-pass-rate report live.

This driver does, per real auto-accept cluster: snapshot_cluster -> compute_merge_projection ->
verify_merge (the 11-check gate), via merge.shadow_merge (which takes NO gateway and STRUCTURALLY
cannot commit). It then aggregates with observability.daily_report and demonstrates query_provenance.

SAFETY — this performs ZERO Zotero mutations:
  * It NEVER touches client.gateway (so no WriteGateway is ever built), NEVER calls merge_cluster /
    commit_merge / rollback_merge / reconcile_orphan_commits, and NEVER sets ZOT_MERGE_LIVE_ENABLED.
  * shadow_merge takes no gateway; dedup_scan/compute_merge_projection/verify_merge are pure.
  * The only writes are to a (by default ISOLATED) local PROV store — the intended audit trail.
  * Reads go through the Zotero Web API v3 (read-only GET); the local API may be offline.

GUARD: refuses to run unless ZOT_EXITGATE_LIVE_GATE=I-UNDERSTAND, and refuses if the live-merge token
is set. Env knobs: ZOT_LIBRARY_ID (default 11056739), EXITGATE_MAX_ITEMS (0=whole library),
EXITGATE_SHADOW_CAP (0=all auto-accept clusters), EXITGATE_DEDUP_ONLY (1=stop after dedup),
EXITGATE_OUT_DIR (where to write the audit report), ZOTERO_PROV_DIR (isolated PROV store).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timezone  # noqa: E402

from zotero_write_mcp import merge as M  # noqa: E402
from zotero_write_mcp.merge_live import WebClusterReader, live_merge_enabled  # noqa: E402
from zotero_write_mcp.dedup import dedup_scan  # noqa: E402
from zotero_write_mcp.observability import (  # noqa: E402
    daily_report, query_provenance, observability_is_fresh, VERIFY_ACTIVITIES,
)
from zotero_write_mcp.merge_live import find_orphan_commit_intents  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402

LIBRARY_ID = int(os.environ.get("ZOT_LIBRARY_ID", "11056739"))
BASE = f"http://zotero.org/users/{LIBRARY_ID}/items"
MAX_ITEMS = int(os.environ.get("EXITGATE_MAX_ITEMS", "0"))      # 0 = whole library
SHADOW_CAP = int(os.environ.get("EXITGATE_SHADOW_CAP", "0"))    # 0 = every auto-accept cluster
DEDUP_ONLY = os.environ.get("EXITGATE_DEDUP_ONLY") == "1"
OUT_DIR = os.environ.get("EXITGATE_OUT_DIR",
                         os.path.join(os.path.dirname(__file__), "..", "exit-gate-runs"))


def _data(it):
    return it.get("data", it)


def web_items(client, item_type="-attachment", page=100, max_items=0):
    """Paginate the live library via the Web API (read-only GET). Respects Backoff/Retry-After."""
    out, start = [], 0
    while True:
        r = client._client.get(
            f"{client.web_url}/users/{LIBRARY_ID}/items",
            headers=client._web_headers,
            params={"limit": page, "start": start, "itemType": item_type, "format": "json"},
        )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "5"))
            print(f"[READ] 429 rate-limited; sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        start += len(batch)
        print(f"[READ] fetched {len(out)} items...", flush=True)
        backoff = r.headers.get("Backoff")
        if backoff:
            time.sleep(int(backoff))
        if max_items and len(out) >= max_items:
            out = out[:max_items]
            break
        if len(batch) < page:
            break
    return out


def main():
    if os.environ.get("ZOT_EXITGATE_LIVE_GATE") != "I-UNDERSTAND":
        print("FAIL: set ZOT_EXITGATE_LIVE_GATE=I-UNDERSTAND (this does LIVE READS on the real library).")
        sys.exit(1)
    if not os.environ.get("ZOTERO_API_KEY"):
        print("FAIL: ZOTERO_API_KEY not set.")
        sys.exit(1)
    # HARD SAFETY: the live-merge token must NOT be set for an exit-gate run.
    if live_merge_enabled():
        print("FAIL: ZOT_MERGE_LIVE_ENABLED is set — refusing to run the exit gate with live merge enabled.")
        sys.exit(1)

    client = ZoteroClient()
    client._library_id = LIBRARY_ID                 # pin id; skip the (offline) local-API auto-detect
    reader = WebClusterReader(client, LIBRARY_ID)   # read-only web reader (?includeTrashed=1 on children)
    prov = client.prov                              # isolated when ZOTERO_PROV_DIR points at a scratch dir
    now = datetime.now(timezone.utc)

    # Read-only crash-recovery sanity: an orphan intent would make reconcile PATCH; we never reconcile,
    # but assert the (isolated) store is clean so verify_pass_rate reflects only this batch.
    orphans = find_orphan_commit_intents(prov)
    if orphans:
        print(f"[WARN] {len(orphans)} orphan commit intent(s) in PROV store (NOT reconciling here).")

    # ── STAGE 1: read the live library + deterministic dedup (pure, zero-write) ──
    print(f"[STAGE1] reading library {LIBRARY_ID} via Web API (max_items={MAX_ITEMS or 'ALL'})...", flush=True)
    items = web_items(client, max_items=MAX_ITEMS)
    by_key = {(_data(it).get("key") or it.get("key")): _data(it) for it in items}
    print(f"[STAGE1] {len(items)} items read; running dedup_scan...", flush=True)
    rep = dedup_scan(items)
    clusters_all = rep["candidate_clusters"]
    auto = [c for c in clusters_all if c.auto_accept]
    print(f"[STAGE1] candidate_clusters={len(clusters_all)}  auto_accept={rep['auto_accept_count']}  "
          f"review={rep['review_count']}  probabilistic={rep['probabilistic_review']}", flush=True)

    def meta(key):
        d = by_key.get(key, {})
        creators = d.get("creators", []) or []
        authors = "; ".join((c.get("lastName") or c.get("name") or "") for c in creators[:3])
        return {"key": key, "itemType": d.get("itemType"), "title": d.get("title"),
                "year": str(d.get("date") or "")[:4], "authors": authors,
                "DOI": d.get("DOI") or d.get("doi") or ""}

    # Build the human-audit table for EVERY auto-accept cluster (this is the load-bearing evidence).
    audit = []
    for c in auto:
        master = c.master_key
        secondaries = [k for k in c.item_keys if k != master]
        audit.append({
            "reason": c.reason,
            "master": meta(master),
            "secondaries": [meta(k) for k in secondaries],
            "shadow": None,  # filled in stage 2
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    report_path = os.path.join(OUT_DIR, f"exit-gate-audit_{stamp}.json")

    if DEDUP_ONLY:
        out = {"mode": "dedup-only", "ts": now.isoformat(), "library_id": LIBRARY_ID,
               "items_scanned": len(items), "auto_accept_count": rep["auto_accept_count"],
               "review_count": rep["review_count"], "clusters": audit}
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print(f"\n[DEDUP-ONLY] {rep['auto_accept_count']} auto-accept cluster(s). Audit table -> {report_path}")
        for i, a in enumerate(audit):
            m = a["master"]
            print(f"  [{i+1}] {a['reason']}")
            print(f"      MASTER  {m['key']} | {m['itemType']} | {m['year']} | {m['authors']} | {m['title']}")
            for s in a["secondaries"]:
                print(f"      dup     {s['key']} | {s['itemType']} | {s['year']} | {s['authors']} | {s['title']}")
        return

    # ── STAGE 2: shadow_merge each auto-accept cluster (ZERO Zotero write) ──
    clusters = auto if not SHADOW_CAP else auto[:SHADOW_CAP]
    if SHADOW_CAP and len(auto) > SHADOW_CAP:
        print(f"[STAGE2] NOTE: capping shadow at {SHADOW_CAP} of {len(auto)} auto-accept clusters (rest NOT verified).")
    failures = []
    for i, c in enumerate(clusters):
        master = c.master_key
        secondaries = [k for k in c.item_keys if k != master]
        try:
            sr = M.shadow_merge(reader, master, secondaries, prov=prov, library_base=BASE)
            ok = bool(sr.passed)
            failed_checks = [ch.name for ch in sr.integrity.failed]
            snap_id = sr.snapshot_id
        except Exception as e:  # a read/structural error counts as a failure to surface
            ok, failed_checks, snap_id = False, [f"EXCEPTION: {type(e).__name__}: {e}"], None
        audit[i]["shadow"] = {"pass": ok, "failed_checks": failed_checks, "snapshot_id": snap_id}
        if not ok:
            failures.append((master, failed_checks))
        print(f"[SHADOW {i+1}/{len(clusters)}] master={master} dups={secondaries} pass={ok}"
              f"{('  FAILED:' + str(failed_checks)) if not ok else ''}", flush=True)

    # ── STAGE 3: observability — aggregate rate + freshness marker + query_provenance demo ──
    drep = daily_report(prov, sample_size=min(10, max(1, len(clusters))), ts=now.isoformat(), pass_rate_floor=1.0)
    fresh = observability_is_fresh(prov, window_seconds=48 * 3600, now=now)
    # Demonstrate query_provenance pulls the per-cluster verdict + before/after blobs for a sample.
    sample_keys = [c.master_key for c in clusters[:3]]
    qp_demo = []
    for mk in sample_keys:
        hist = query_provenance(prov, mk)
        verdicts = [h for h in hist if h["activity"] in VERIFY_ACTIVITIES
                    and isinstance(h.get("params"), dict) and "pass" in h["params"]]
        if verdicts:
            last = verdicts[-1]
            qp_demo.append({"item_key": mk, "activity": last["activity"], "pass": last["params"]["pass"],
                            "has_before_blob": bool(last["reversibility"].get("before_blob")),
                            "has_after_blob": bool(last["reversibility"].get("after_blob"))})

    out = {
        "mode": "exit-gate-shadow",
        "ts": now.isoformat(),
        "library_id": LIBRARY_ID,
        "items_scanned": len(items),
        "auto_accept_count": rep["auto_accept_count"],
        "review_count": rep["review_count"],
        "clusters_verified": len(clusters),
        "clusters_capped_out": len(auto) - len(clusters),
        "metrics": {k: drep[k] for k in
                    ("verify_pass_rate", "verify_total", "verify_passed", "merges_committed",
                     "prov_records", "pass_rate_floor", "status")},
        "observability_fresh": fresh,
        "query_provenance_demo": qp_demo,
        "shadow_failures": failures,
        "clusters": audit,
        "notes": [
            "shadow_merge verifies the COMPUTED PROJECTION (self-consistent) — 100% pass proves the "
            "pipeline + 11-check gate + PROV/observability wiring run cleanly over real clusters, not a "
            "live post-PATCH drift catch (that is covered by test_verify_injection + the phase-2 live smoke).",
            "verify check #11 (citekey-preservation) is not meaningfully exercised: WebClusterReader.get_citekey "
            "returns None (no BBT JSON-RPC), so snapshot==observed citekey (None==None) passes trivially.",
            "ZERO Zotero mutations: no gateway access, no merge_cluster/commit_merge, token unset.",
        ],
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    m = out["metrics"]
    print("\n" + "=" * 78)
    print(f"EXIT-GATE SHADOW RESULT  (library {LIBRARY_ID}, {len(items)} items scanned)")
    print(f"  auto-accept clusters : {rep['auto_accept_count']}  (verified {len(clusters)}, "
          f"capped-out {len(auto) - len(clusters)})")
    print(f"  verify_pass_rate     : {m['verify_pass_rate']}  (passed {m['verify_passed']}/{m['verify_total']})")
    print(f"  observability status : {m['status']}   fresh={fresh}")
    print(f"  shadow failures      : {len(failures)}")
    print(f"  audit report         : {report_path}")
    gate_ok = (m["verify_pass_rate"] == 1.0 and m["verify_total"] == len(clusters)
               and m["status"] == "ok" and not failures)
    print(f"  MACHINE GATE         : {'PASS' if gate_ok else 'NOT PASSED'} "
          f"(verify_pass_rate==1.0 over all {len(clusters)} clusters, status ok, no failures)")
    print("  HUMAN GATE           : owner audit of the cluster table for ZERO false merges — REQUIRED before sign-off.")
    print("=" * 78)
    sys.exit(0 if gate_ok else 2)


if __name__ == "__main__":
    main()
