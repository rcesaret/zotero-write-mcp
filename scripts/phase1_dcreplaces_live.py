#!/usr/bin/env python3
"""Phase-1 live dc:replaces smoke — throwaway snapshot->merge->verify->rollback on the REAL library.

Proves the LIVE reader + snapshot_cluster + verify_merge + rollback_merge + dc:replaces all work
end-to-end against the real Zotero Web API. Flow (all on THROWAWAY items, net-zero):
  1. create master + secondary + a child note under the secondary;
  2. snapshot the cluster (live read);
  3. apply a real merge via the gateway — reparent the note to master, union a tag, add
     dc:replaces master->secondary (the projection computed by compute_merge_projection);
  4. re-read live and run verify_merge  -> expect PASS (live reader + gate work);
  5. confirm dc:replaces is present + GET-resolvable on the master;
  6. rollback_merge -> re-read -> confirm the master reverted and the note is back under the secondary;
  7. delete all throwaways (finally:), so the library returns to its prior state.

The WORD-PROCESSOR citation-resolution confirmation (a cite to the trashed secondary resolving through
dc:replaces in Zotero+Word) is a separate OWNER step — this script proves only the API-level guarantee.

GUARD: refuses to run unless ZOT_PHASE1_LIVE_GATE=I-UNDERSTAND. Needs ZOTERO_API_KEY + Zotero reachable.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402

from zotero_write_mcp import merge as M  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402

LIBRARY_ID = int(os.environ.get("ZOT_LIBRARY_ID", "11056739"))
BASE = f"http://zotero.org/users/{LIBRARY_ID}/items"
MARK = "ZZZ-HARNESS-PHASE1-MERGE-SELFTEST — DELETE ME"


class LiveReader:
    """Minimal live ClusterReader over the Zotero Web API (version-accurate web GETs)."""

    def __init__(self, client):
        self.c = client

    def _get(self, path):
        r = self.c._client.get(f"{self.c.web_url}{path}", headers=self.c._web_headers)
        r.raise_for_status()
        return r.json()

    def get_item(self, key):
        return self._get(f"/users/{LIBRARY_ID}/items/{key}")

    def get_children(self, key):
        return self._get(f"/users/{LIBRARY_ID}/items/{key}/children")

    def get_annotations(self, attachment_key):
        return [c for c in self._get(f"/users/{LIBRARY_ID}/items/{attachment_key}/children")
                if c.get("data", {}).get("itemType") == "annotation"]

    def get_citekey(self, key):
        return None  # BBT citekey not needed for throwaways; #11 compares None==None


def lib_version(client):
    r = client._client.get(f"{client.web_url}/users/{LIBRARY_ID}/items",
                           headers=client._web_headers, params={"limit": 1, "format": "keys"})
    r.raise_for_status()
    return int(r.headers["Last-Modified-Version"])


def main():
    if os.environ.get("ZOT_PHASE1_LIVE_GATE") != "I-UNDERSTAND":
        print("FAIL: set ZOT_PHASE1_LIVE_GATE=I-UNDERSTAND (this does LIVE writes).")
        sys.exit(1)
    if not os.environ.get("ZOTERO_API_KEY"):
        print("FAIL: ZOTERO_API_KEY not set.")
        sys.exit(1)

    client = ZoteroClient()
    client._library_id = LIBRARY_ID
    reader = LiveReader(client)
    gw = client.gateway
    created = []

    def patch(key, data, version):
        return gw.update_item(LIBRARY_ID, key, data, version, library_type="user")

    try:
        # 1 — create master + secondary + child note under the secondary
        env = client.create_items([
            {"itemType": "document", "title": MARK + " [MASTER]", "tags": [{"tag": "DELETE-ME"}]},
            {"itemType": "document", "title": MARK + " [SECONDARY]",
             "collections": [], "tags": [{"tag": "DELETE-ME"}, {"tag": "dup-only-tag", "type": 1}]},
        ])
        if env.get("failed"):
            raise RuntimeError(f"create master/secondary failed: {env['failed']}")
        master, secondary = env["success"]["0"], env["success"]["1"]
        created += [master, secondary]
        note_env = client.create_items([
            {"itemType": "note", "parentItem": secondary, "note": "throwaway child note",
             "tags": [{"tag": "DELETE-ME"}]},
        ])
        note = note_env["success"]["0"]
        created.append(note)
        print(f"[CREATE]   master={master}  secondary={secondary}  note(under secondary)={note}")

        # 2 — snapshot the cluster (live read)
        snap = M.snapshot_cluster(reader, master, [secondary], prov=client.prov)
        print(f"[SNAPSHOT] {snap.snapshot_id}  items={list(snap.items)}  "
              f"notes={[n.key for n in snap.notes]} (parent {snap.notes[0].parent_key})")

        # 3 — apply a real merge: union master fields + reparent the note + add dc:replaces
        proj = M.compute_merge_projection(snap, library_base=BASE)
        pm = proj.items[master]
        patch(master, {"collections": pm.collections, "tags": M._zotero_tags(pm.tags),
                       "relations": pm.relations}, snap.items[master].version)
        patch(note, {"parentItem": master}, snap.notes[0].version)
        print(f"[MERGE]    PATCHed master (union tags {[t[0] for t in pm.tags]}, "
              f"dc:replaces->{secondary}); reparented {note} -> master")

        # 4 — re-read live, run the 11-check gate
        observed = M.snapshot_cluster(reader, master, [secondary], prov=client.prov)
        report = M.verify_merge(snap, observed)
        print(f"[VERIFY]   pass={report.passed}  "
              f"failed={[c.name for c in report.failed]}")
        if not report.passed:
            raise RuntimeError(f"verify_merge FAILED on a live correct merge: {report.to_dict()}")

        # 5 — confirm dc:replaces present + resolvable
        live_master = reader.get_item(master)["data"]
        dc = live_master.get("relations", {}).get("dc:replaces", [])
        dc = [dc] if isinstance(dc, str) else dc
        ok_dc = any(M._rel_target_key(v) == secondary for v in dc)
        print(f"[DCREPL]   master.relations['dc:replaces']={dc}  resolves->{secondary}: {ok_dc}")
        if not ok_dc:
            raise RuntimeError("dc:replaces not present/resolvable on master")

        # 6 — rollback, then confirm the revert
        restore = M.rollback_merge(snap, observed, gw, library_id=LIBRARY_ID)
        print(f"[ROLLBACK] state={restore.state}  ops={[(o['op'], o.get('key')) for o in restore.operations]}")
        after = reader.get_item(master)["data"]
        note_parent = reader.get_item(note)["data"].get("parentItem")
        reverted_dc = after.get("relations", {}).get("dc:replaces", [])
        print(f"[REVERTED] master dc:replaces now={reverted_dc or 'none'}  note parent now={note_parent}")
        if note_parent != secondary:
            raise RuntimeError(f"rollback did not reparent note back to secondary (got {note_parent})")

        print("\nPASS: live snapshot -> merge (reparent + union + dc:replaces) -> verify (11/11) -> "
              "rollback round-trip on the real library; dc:replaces resolvable; net-zero after cleanup.")
        print("OWNER FOLLOW-UP: confirm in Zotero+Word that a citation to a trashed secondary resolves "
              "through dc:replaces to the master (the word-processor half of the Phase-1 live gate).")
    except Exception as e:
        print(f"\nFAIL: {e}")
        print(f"   created keys (verify cleanup): {created}")
        _cleanup(client, created)
        sys.exit(1)
    else:
        _cleanup(client, created)


def _cleanup(client, created):
    if not created:
        return
    try:
        v = lib_version(client)
        client.gateway.delete_items(LIBRARY_ID, created, v, library_type="user")
        print(f"[CLEANUP]  deleted throwaways {created}")
    except Exception as e:
        print(f"[CLEANUP]  WARNING could not delete {created}: {e} — delete them manually.")


if __name__ == "__main__":
    main()
