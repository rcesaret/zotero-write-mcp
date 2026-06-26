#!/usr/bin/env python3
"""Phase-2 APPLY the owner-approved reconciliation over the live auto-accept clusters — SHADOW by default.

For every current deterministic auto-accept cluster, looks up its approved field-level reconciliation
(``reconciled-records-153.json``, matched by member-key set) and runs snapshot -> compute_merge_projection
-> verify_merge WITH the field_sources via merge.shadow_merge — which takes NO gateway and structurally
cannot write. Produces a per-cluster PREVIEW (the projected enriched survivor + the dup keys that would be
trashed + the verify verdict) for owner review BEFORE any live merge. Reads the live library via the Web
API; the only writes are local PROV. NO Zotero mutation, NO trash, NO token.

GUARD: refuses unless ZOT_APPLY_GATE=I-UNDERSTAND. Env: ZOT_LIBRARY_ID (11056739), RECON_JSON (path to the
reconciled records), APPLY_OUT_DIR, ZOTERO_PROV_DIR.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zotero_write_mcp import merge as M  # noqa: E402
from zotero_write_mcp.merge_live import WebClusterReader, live_merge_enabled  # noqa: E402
from zotero_write_mcp.dedup import dedup_scan  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402

LIBRARY_ID = int(os.environ.get("ZOT_LIBRARY_ID", "11056739"))
BASE = f"http://zotero.org/users/{LIBRARY_ID}/items"
RECON_JSON = os.environ.get("RECON_JSON",
                            os.path.join(os.path.dirname(__file__), "..", "exit-gate-runs", "reconciled-records-153.json"))
OUT_DIR = os.environ.get("APPLY_OUT_DIR", os.path.join(os.path.dirname(__file__), "..", "exit-gate-runs", "apply"))


def _data(it):
    return it.get("data", it)


def web_items(client, item_type="-attachment", page=100):
    out, start = [], 0
    while True:
        r = client._client.get(f"{client.web_url}/users/{LIBRARY_ID}/items", headers=client._web_headers,
                               params={"limit": page, "start": start, "itemType": item_type, "format": "json"})
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5"))); continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch); start += len(batch)
        if r.headers.get("Backoff"):
            time.sleep(int(r.headers["Backoff"]))
        if len(batch) < page:
            break
    return out


def main():
    if os.environ.get("ZOT_APPLY_GATE") != "I-UNDERSTAND":
        print("FAIL: set ZOT_APPLY_GATE=I-UNDERSTAND (LIVE READS on the real library; shadow only — no writes).")
        sys.exit(1)
    if not os.environ.get("ZOTERO_API_KEY"):
        print("FAIL: ZOTERO_API_KEY not set."); sys.exit(1)
    if live_merge_enabled():
        print("FAIL: ZOT_MERGE_LIVE_ENABLED is set — this driver is SHADOW-only; unset it."); sys.exit(1)

    # field_sources by member-key set, from the approved reconciliation
    recon = json.load(open(RECON_JSON, encoding="utf-8"))["clusters"]
    fs_by_members = {}
    for c in recon:
        fs = {f: info["source_key"] for f, info in c.get("fields", {}).items()}
        fs_by_members[frozenset(c["member_keys"])] = {"field_sources": fs, "is_duplicate": c.get("is_duplicate")}
    print(f"[RECON]    loaded {len(fs_by_members)} approved reconciliations from {os.path.basename(RECON_JSON)}")

    client = ZoteroClient(); client._library_id = LIBRARY_ID
    reader = WebClusterReader(client, LIBRARY_ID)
    prov = client.prov

    print(f"[SCAN]     reading library {LIBRARY_ID} + dedup_scan ...", flush=True)
    items = web_items(client)
    by_key = {(_data(it).get("key") or it.get("key")): _data(it) for it in items}
    rep = dedup_scan(items)
    auto = [c for c in rep["candidate_clusters"] if c.auto_accept]
    print(f"[SCAN]     {len(items)} items; {len(auto)} auto-accept clusters", flush=True)

    preview, failures, unmatched = [], [], []
    for i, c in enumerate(auto):
        master = c.master_key
        secondaries = [k for k in c.item_keys if k != master]
        rec = fs_by_members.get(frozenset(c.item_keys))
        fs = rec["field_sources"] if rec else {}
        if rec is None:
            unmatched.append(master)
        try:
            sr = M.shadow_merge(reader, master, secondaries, prov=prov, field_sources=fs, library_base=BASE)
            ok = bool(sr.passed)
            failed = [ch.name for ch in sr.integrity.failed]
            pm = sr.projection.items[master].fields
            sm = by_key.get(master, {})
            # the survivor changes this merge would make
            changes = {f: {"from": sm.get(f), "to": pm.get(f)} for f in set(list(fs) + ["extra"])
                       if pm.get(f) != sm.get(f)}
        except Exception as e:
            ok, failed, changes, pm = False, [f"EXCEPTION: {type(e).__name__}: {e}"], {}, {}
        rec_out = {"master": master, "trash": secondaries, "is_duplicate": rec["is_duplicate"] if rec else "UNMATCHED",
                   "verify_pass": ok, "failed_checks": failed, "survivor_changes": changes,
                   "title": (by_key.get(master, {}).get("title"))}
        preview.append(rec_out)
        if not ok:
            failures.append((master, failed))
        if (i + 1) % 25 == 0:
            print(f"[SHADOW]   {i + 1}/{len(auto)} ...", flush=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "apply-preview.json")
    json.dump({"library_id": LIBRARY_ID, "auto_accept": len(auto), "verify_passed": len(auto) - len(failures),
               "failures": failures, "unmatched": unmatched, "clusters": preview},
              open(out_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    enriched = sum(1 for p in preview if any(f != "extra" for f in p["survivor_changes"]))
    aliased = sum(1 for p in preview if "extra" in p["survivor_changes"])
    print("\n" + "=" * 78)
    print(f"APPLY PREVIEW (SHADOW — zero writes)  ·  {len(auto)} auto-accept clusters")
    print(f"  verify pass         : {len(auto) - len(failures)}/{len(auto)}")
    print(f"  with field enrichment: {enriched}    with citekey alias: {aliased}")
    print(f"  reconciliations matched: {len(auto) - len(unmatched)}/{len(auto)}  (unmatched -> no enrichment: {len(unmatched)})")
    print(f"  shadow failures     : {len(failures)}  {failures[:5] if failures else ''}")
    print(f"  preview written     : {out_path}")
    gate = not failures
    print(f"  RESULT              : {'ALL CLUSTERS PROJECT+VERIFY CLEAN' if gate else 'FAILURES — review before live'}")
    print("=" * 78)
    sys.exit(0 if gate else 2)


if __name__ == "__main__":
    main()
