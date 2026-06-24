#!/usr/bin/env python3
"""Phase-2 live commit_merge smoke — throwaway snapshot->merge->COMMIT(trash)->rollback on the REAL library.

The first live TRASH. Proves the full gated chain end-to-end against the real Zotero Web API, on THROWAWAY
items, net-zero:
  1. create a throwaway duplicate cluster: master + secondary + a child note under the secondary;
  2. snapshot_cluster (live reader, includeTrashed children);
  3. merge_cluster -> PATCH phase (reparent note, union tags, dc:replaces master->secondary);
  4. daily_report -> fresh observability marker;
  5. ENABLE the live token (process-local) and commit_merge -> it re-verifies (11 checks) and TRASHES the
     secondary via PATCH deleted:1 (TRASH-NOT-PURGE) -> confirm mode=committed, secondary deleted:1 + present;
  6. rollback_merge -> un-trash secondary, revert master, reparent the note back -> confirm restore;
  7. hard-delete the throwaways (cleanup).

The enable token is set ONLY inside this process for this one controlled cluster; it does NOT persist to the
harness MCP server. GUARD: refuses to run unless ZOT_PHASE2_LIVE_GATE=I-UNDERSTAND.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timezone  # noqa: E402

import httpx  # noqa: E402

from zotero_write_mcp import merge as M  # noqa: E402
from zotero_write_mcp.merge_live import (  # noqa: E402
    merge_cluster, commit_merge, ENABLE_ENV, ENABLE_TOKEN,
)
from zotero_write_mcp.observability import daily_report  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402

LIBRARY_ID = int(os.environ.get("ZOT_LIBRARY_ID", "11056739"))
MARK = "ZZZ-HARNESS-PHASE2-COMMIT-SELFTEST — DELETE ME"


class WebClusterReader:
    """Live ClusterReader over the Zotero Web API. get_children includes TRASHED children (?includeTrashed=1)
    so M-4 / the terminal verify can see a cascade-trashed child."""

    def __init__(self, client):
        self.c = client

    def _get(self, path, params=None):
        r = self.c._client.get(f"{self.c.web_url}{path}", headers=self.c._web_headers, params=params or {})
        r.raise_for_status()
        return r.json()

    def get_item(self, key):
        return self._get(f"/users/{LIBRARY_ID}/items/{key}")

    def get_children(self, key):
        return self._get(f"/users/{LIBRARY_ID}/items/{key}/children", {"includeTrashed": 1})

    def get_annotations(self, attachment_key):
        return [c for c in self.get_children(attachment_key)
                if c.get("data", {}).get("itemType") == "annotation"]

    def get_citekey(self, key):
        return None


def fail(msg, created, client):
    print(f"\nFAIL: {msg}")
    print(f"   created keys (verify cleanup): {created}")
    _cleanup(client, created)
    sys.exit(1)


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
        print("FAIL: set ZOT_PHASE2_LIVE_GATE=I-UNDERSTAND (this does LIVE writes incl. a trash).")
        sys.exit(1)
    if not os.environ.get("ZOTERO_API_KEY"):
        print("FAIL: ZOTERO_API_KEY not set.")
        sys.exit(1)

    client = ZoteroClient()
    client._library_id = LIBRARY_ID
    reader = WebClusterReader(client)
    gw = client.gateway
    prov = client.prov
    created = []
    now = datetime.now(timezone.utc)

    try:
        # 1 — throwaway duplicate cluster: master + secondary + child note under the secondary
        env = client.create_items([
            {"itemType": "document", "title": MARK + " [MASTER]",
             "tags": [{"tag": "DELETE-ME"}, {"tag": "zz-master-tag"}]},
            {"itemType": "document", "title": MARK + " [SECONDARY]",
             "tags": [{"tag": "DELETE-ME"}, {"tag": "zz-secondary-tag"}]},
        ])
        if env.get("failed"):
            raise RuntimeError(f"create master/secondary failed: {env['failed']}")
        master, secondary = env["success"]["0"], env["success"]["1"]
        created += [master, secondary]
        note = client.create_items([{"itemType": "note", "parentItem": secondary,
                                     "note": "throwaway child note", "tags": [{"tag": "DELETE-ME"}]}])["success"]["0"]
        created.append(note)
        print(f"[CREATE]   master={master}  secondary={secondary}  note(under secondary)={note}")

        # 2 — snapshot, 3 — merge (PATCH phase)
        snap = M.snapshot_cluster(reader, master, [secondary], prov=prov)
        plan = merge_cluster(snap, reader, gw, library_id=LIBRARY_ID)
        if plan.drifted:
            raise RuntimeError(f"merge_cluster drifted: {plan.drift_keys}")
        print(f"[MERGE]    PATCHed master (union tags + dc:replaces->{secondary}); reparented {note} -> master")

        # 4 — fresh observability marker
        daily_report(prov, ts=now.isoformat())

        # 5 — ENABLE live (process-local) + commit (the gated TRASH)
        os.environ[ENABLE_ENV] = ENABLE_TOKEN
        res = commit_merge(snap, reader, gw, prov, library_id=LIBRARY_ID, now=now)
        print(f"[COMMIT]   mode={res.mode}  trashed={res.trashed}")
        if res.mode != "committed":
            raise RuntimeError(f"commit_merge did not commit: mode={res.mode} reason={res.reason}")

        sec_live = reader.get_item(secondary)["data"]
        master_live = reader.get_item(master)["data"]
        note_parent = reader.get_item(note)["data"].get("parentItem")
        trashed_ok = sec_live.get("deleted") in (1, "1", True)
        dc = master_live.get("relations", {}).get("dc:replaces", [])
        dc = [dc] if isinstance(dc, str) else dc
        dc_ok = any(M._rel_target_key(v) == secondary for v in dc)
        print(f"[VERIFY]   secondary trashed(not purged)={trashed_ok}  master dc:replaces->{secondary}={dc_ok}  "
              f"note parent now={note_parent}")
        if not (trashed_ok and dc_ok and note_parent == master):
            raise RuntimeError("post-commit state wrong (trashed/dc:replaces/note-parent)")

        # 6 — rollback the live commit and confirm restoration
        observed = M.build_cluster(reader, master, [secondary])
        rb = M.rollback_merge(snap, observed, gw, library_id=LIBRARY_ID)
        print(f"[ROLLBACK] state={rb.state}  ok={rb.ok}  ops={[(o['op'], o.get('key')) for o in rb.operations]}")
        sec_after = reader.get_item(secondary)["data"]
        note_after = reader.get_item(note)["data"].get("parentItem")
        restored = sec_after.get("deleted") in (None, 0) and note_after == secondary
        print(f"[RESTORED] secondary un-trashed={sec_after.get('deleted') in (None, 0)}  note parent now={note_after}")
        if not (rb.ok and restored):
            raise RuntimeError(f"rollback did not restore (ok={rb.ok}, secondary deleted={sec_after.get('deleted')}, "
                               f"note parent={note_after})")

        print("\nPASS: live snapshot -> merge -> COMMIT (verify 11/11 -> trash deleted:1, not purge) -> rollback "
              "(un-trash + revert + reparent) on the real library; throwaways deleted (net-zero).")
    except Exception as e:
        fail(str(e), created, client)
    else:
        _cleanup(client, created)
    finally:
        os.environ.pop(ENABLE_ENV, None)   # never leave the live token set


if __name__ == "__main__":
    main()
