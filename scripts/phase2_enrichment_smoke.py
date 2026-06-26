#!/usr/bin/env python3
"""Phase-2 field-enrichment + re-parent live smoke — THROWAWAY items, net-zero.

Proves on the REAL Zotero Web API (throwaways only), end to end:
  - field-level metadata ENRICHMENT (Phase B): the survivor takes a fuller title + a publisher + a place
    from the secondary, applied by merge_cluster and VERIFY-GATED by commit_merge;
  - NOTE and ATTACHMENT (imported_file) re-parent to the survivor — the owner's "is it a real MERGE, not
    a trash?" concern: children are preserved under the survivor, not orphaned;
  - dc:replaces survivor->secondary; trash-NOT-purge; and rollback_merge that FULLY restores everything,
    including reverting the enrichment (the survivor's title back, the added publisher/place cleared).

GUARD: refuses unless ZOT_PHASE2_LIVE_GATE=I-UNDERSTAND. The enable token is set process-local and popped
in finally. Citation-key ALIAS accumulation is NOT tested here (needs BBT JSON-RPC / a running Zotero).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timezone  # noqa: E402

from zotero_write_mcp import merge as M  # noqa: E402
from zotero_write_mcp.merge_live import (  # noqa: E402
    merge_cluster, commit_merge, WebClusterReader, ENABLE_ENV, ENABLE_TOKEN,
)
from zotero_write_mcp.observability import daily_report  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402

LIBRARY_ID = int(os.environ.get("ZOT_LIBRARY_ID", "11056739"))
MARK = "ZZZ-HARNESS-ENRICH-SMOKE — DELETE ME"
FULL = MARK + " Reino: A Full Enriched Subtitle"
SHORT = MARK + " Reino"


def _lib_version(client):
    r = client._client.get(f"{client.web_url}/users/{LIBRARY_ID}/items",
                           headers=client._web_headers, params={"limit": 1, "format": "keys"})
    r.raise_for_status()
    return int(r.headers["Last-Modified-Version"])


def _cleanup(client, created):
    if not created:
        return
    try:
        client.gateway.delete_items(LIBRARY_ID, created, _lib_version(client), library_type="user")
        print(f"[CLEANUP]  deleted throwaways {created}")
    except Exception as e:
        print(f"[CLEANUP]  WARNING could not delete {created}: {e} — delete them manually.")


def main():
    if os.environ.get("ZOT_PHASE2_LIVE_GATE") != "I-UNDERSTAND":
        print("FAIL: set ZOT_PHASE2_LIVE_GATE=I-UNDERSTAND (this does LIVE throwaway writes incl. a trash).")
        sys.exit(1)
    if not os.environ.get("ZOTERO_API_KEY"):
        print("FAIL: ZOTERO_API_KEY not set.")
        sys.exit(1)

    client = ZoteroClient()
    client._library_id = LIBRARY_ID
    reader = WebClusterReader(client, LIBRARY_ID)
    gw, prov = client.gateway, client.prov
    created = []
    now = datetime.now(timezone.utc)

    try:
        # 1 — throwaway cluster: survivor (SHORT title, no publisher/place) + secondary (FULL title + pub + place)
        env = client.create_items([
            {"itemType": "book", "title": SHORT, "tags": [{"tag": "DELETE-ME"}]},
            {"itemType": "book", "title": FULL, "publisher": "ZZZ Smoke Press", "place": "Smoketown",
             "citationKey": "zzsmokedup1979", "tags": [{"tag": "DELETE-ME"}]},
        ])
        if env.get("failed"):
            raise RuntimeError(f"create survivor/secondary failed: {env['failed']}")
        master, secondary = env["success"]["0"], env["success"]["1"]
        created += [master, secondary]
        kids = client.create_items([
            {"itemType": "note", "parentItem": secondary, "note": "throwaway note", "tags": [{"tag": "DELETE-ME"}]},
            {"itemType": "attachment", "parentItem": secondary, "linkMode": "imported_file",
             "title": "smoke.pdf", "filename": "smoke.pdf", "contentType": "application/pdf",
             "tags": [{"tag": "DELETE-ME"}]},
        ])
        if kids.get("failed"):
            raise RuntimeError(f"create children failed: {kids['failed']}")
        note, att = kids["success"]["0"], kids["success"]["1"]
        created += [note, att]
        print(f"[CREATE]   survivor={master} secondary={secondary} note(under sec)={note} att(under sec)={att}")

        # 2 — snapshot, 3 — field_sources: take title + publisher + place from the SECONDARY
        snap = M.snapshot_cluster(reader, master, [secondary], prov=prov)
        sec_ck = snap.items[secondary].fields.get("citationKey")
        test_alias = bool(sec_ck)
        print(f"[CITEKEY]  secondary citationKey via API = {sec_ck!r}  (alias test enabled: {test_alias})")
        fs = {"title": secondary, "publisher": secondary, "place": secondary}
        plan = merge_cluster(snap, reader, gw, library_id=LIBRARY_ID, field_sources=fs)
        if plan.drifted:
            raise RuntimeError(f"merge_cluster drifted: {plan.drift_keys}")

        md = reader.get_item(master)["data"]
        nd = reader.get_item(note)["data"]
        ad = reader.get_item(att)["data"]
        enr_ok = (md.get("title") == FULL and md.get("publisher") == "ZZZ Smoke Press"
                  and md.get("place") == "Smoketown")
        reparent_ok = nd.get("parentItem") == master and ad.get("parentItem") == master
        print(f"[ENRICH]   survivor title -> '{md.get('title')}' | publisher='{md.get('publisher')}' | "
              f"place='{md.get('place')}'  -> {enr_ok}")
        print(f"[REPARENT] note.parent={nd.get('parentItem')} att.parent={ad.get('parentItem')}  -> {reparent_ok}")
        if not (enr_ok and reparent_ok):
            raise RuntimeError("enrichment or re-parent wrong after merge_cluster")
        alias_ok = (not test_alias) or (f"tex.ids: {sec_ck}" in (md.get("extra") or ""))
        print(f"[ALIAS]    survivor extra={md.get('extra')!r}  -> "
              f"{'PASS' if (alias_ok and test_alias) else ('SKIPPED (no API citekey)' if not test_alias else 'FAIL')}")
        if not alias_ok:
            raise RuntimeError("citekey alias (tex.ids) not written to survivor extra")

        # 4 — fresh observability, 5 — verify-gated commit (the 11-check verify runs WITH field_sources)
        daily_report(prov, ts=now.isoformat())
        os.environ[ENABLE_ENV] = ENABLE_TOKEN
        res = commit_merge(snap, reader, gw, prov, library_id=LIBRARY_ID, field_sources=fs, now=now)
        print(f"[COMMIT]   mode={res.mode} verify_passed={res.verify_passed} trashed={res.trashed}")
        if res.mode != "committed":
            raise RuntimeError(f"commit_merge did not commit: mode={res.mode} reason={res.reason}")

        sd = reader.get_item(secondary)["data"]
        md2 = reader.get_item(master)["data"]
        trashed_ok = sd.get("deleted") in (1, "1", True)
        dc = md2.get("relations", {}).get("dc:replaces", [])
        dc = [dc] if isinstance(dc, str) else dc
        dc_ok = any(M._rel_target_key(v) == secondary for v in dc)
        print(f"[VERIFY]   secondary trashed(not purged)={trashed_ok} | survivor dc:replaces->{secondary}={dc_ok} "
              f"| survivor still enriched title='{md2.get('title')}'")
        if not (trashed_ok and dc_ok and md2.get("title") == FULL):
            raise RuntimeError("post-commit state wrong (trash / dc:replaces / enrichment lost)")

        # 6 — rollback: un-trash secondary, re-parent children back, REVERT the enrichment (title back; pub/place cleared)
        observed = M.build_cluster(reader, master, [secondary])
        rb = M.rollback_merge(snap, observed, gw, library_id=LIBRARY_ID)
        md3 = reader.get_item(master)["data"]
        nd3 = reader.get_item(note)["data"]
        sd3 = reader.get_item(secondary)["data"]
        restored = (rb.ok and sd3.get("deleted") in (None, 0) and nd3.get("parentItem") == secondary
                    and md3.get("title") == SHORT and not md3.get("publisher") and not md3.get("place")
                    and (not test_alias or not md3.get("extra")))
        print(f"[ROLLBACK] ok={rb.ok} secondary un-trashed={sd3.get('deleted') in (None, 0)} "
              f"note re-parented back={nd3.get('parentItem') == secondary}")
        print(f"[REVERT]   survivor title -> '{md3.get('title')}' | publisher cleared={not md3.get('publisher')} "
              f"| place cleared={not md3.get('place')}")
        if not restored:
            raise RuntimeError(f"rollback did not fully restore (incl. enrichment revert): ok={rb.ok}")

        print("\nPASS: field enrichment (title+publisher+place from the secondary, verify-gated) + note & "
              "imported_file attachment re-parent + dc:replaces + trash-NOT-purge + full rollback (enrichment "
              "reverted) on the real library; throwaways deleted (net-zero).")
    except Exception as e:
        print(f"\nFAIL: {e}\n   created (verify cleanup): {created}")
        _cleanup(client, created)
        sys.exit(1)
    else:
        _cleanup(client, created)
    finally:
        os.environ.pop(ENABLE_ENV, None)   # never leave the live token set


if __name__ == "__main__":
    main()
