#!/usr/bin/env python3
"""Phase-0 EXIT-GATE live verification — gateway round-trip on the REAL Zotero library.

Creates -> updates -> deletes ONE clearly-marked THROWAWAY item through the write gateway v2 (the
exact `ZoteroClient.create_items / update_item / delete_item` path the MCP tools use), and proves
every mutation landed in the PROV store. Net effect on the library: zero (the item is deleted).

This is the live half of the Phase-0 exit gate: "Gateway round-trips create/update/delete with
structured envelope + version handling ... every mutation appears in PROV. No mutation path bypasses
the gateway or PROV." (No separate test library exists — decision Q8 — so a throwaway item in the
real library, created and immediately deleted, stands in.)

⚠ THIS PERFORMS LIVE WRITES to the real library. It refuses to run unless
   ZOT_PHASE0_LIVE_GATE=I-UNDERSTAND  is set.

Env:
  ZOTERO_API_KEY        (required)  write-enabled key
  ZOT_PHASE0_LIVE_GATE  (required)  must equal 'I-UNDERSTAND'
  ZOTERO_PROV_DIR       (optional)  where PROV is written; default ~/.zotero-write-mcp/prov
  ZOT_LIBRARY_ID        (optional)  default 11056739 (the owner's user library)

Run (from the engine repo root):
  ZOT_PHASE0_LIVE_GATE=I-UNDERSTAND ZOTERO_PROV_DIR=../../runtime/prov-phase0-livegate \\
    uv run --no-project --with httpx python scripts/phase0_live_gate.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402

from zotero_write_mcp import __version__  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402
from zotero_write_mcp.gateway import ConcurrencyConflictError  # noqa: E402

LIBRARY_ID = int(os.environ.get("ZOT_LIBRARY_ID", "11056739"))
MARKER = "ZZZ-HARNESS-PHASE0-GATEWAY-SELFTEST — DELETE ME"


def fail(msg: str) -> "None":
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def current_library_version(client: ZoteroClient) -> int:
    """Freshest library version from the Web API (robust against concurrent desktop sync)."""
    r = client._client.get(
        f"{client.web_url}/users/{LIBRARY_ID}/items",
        headers=client._web_headers, params={"limit": 1, "format": "keys"})
    r.raise_for_status()
    return int(r.headers["Last-Modified-Version"])


def main() -> "None":
    if os.environ.get("ZOT_PHASE0_LIVE_GATE") != "I-UNDERSTAND":
        fail("refusing to run: set ZOT_PHASE0_LIVE_GATE=I-UNDERSTAND (this does LIVE writes).")
    if not os.environ.get("ZOTERO_API_KEY"):
        fail("ZOTERO_API_KEY not set (required for writes).")

    client = ZoteroClient()
    client._library_id = LIBRARY_ID  # bypass local-API auto-detect; cloud Web API only
    prov = client.prov
    print(f"engine __version__ : {__version__}")
    print(f"library            : users/{LIBRARY_ID}")
    print(f"PROV store         : {prov.root}")

    # ── 1. CREATE ─────────────────────────────────────────────────────────────
    obj = {
        "itemType": "document",
        "title": MARKER,
        "extra": "Throwaway item from the Phase-0 gateway exit-gate self-test. Safe to delete.",
        "tags": [{"tag": "DELETE-ME"}],
    }
    create_env = client.create_items([obj])
    if create_env.get("failed"):
        fail(f"create failed: {create_env['failed']}")
    keys = list(create_env["success"].values())
    if not keys:
        fail(f"create returned no key: {create_env}")
    key = keys[0]
    v_create = create_env["last_modified_version"]
    print(f"\n[CREATE]  key={key}  library_version={v_create}  -> ok")

    # ── 2. UPDATE (PATCH; versioned; gateway self-heals on 412) ───────────────
    update_res = client.update_item(key, {"title": MARKER + " [UPDATED]"}, v_create)
    v_update = update_res.last_modified_version
    print(f"[UPDATE]  key={key}  http={update_res.status_code}  new_version={v_update}  -> ok")

    # ── 3. DELETE (versioned) ─────────────────────────────────────────────────
    lib_v = current_library_version(client)
    try:
        client.delete_item(key, lib_v)
    except ConcurrencyConflictError:
        lib_v = current_library_version(client)  # library advanced; re-read + retry once
        client.delete_item(key, lib_v)
    print(f"[DELETE]  key={key}  library_version_used={lib_v}  -> ok")

    # ── 4. VERIFY the item is gone (web GET -> 404) ───────────────────────────
    time.sleep(1.0)  # read-your-writes is immediate, but a small cushion costs nothing
    try:
        client.get_item_web(key)
        deleted = False
    except httpx.HTTPStatusError as e:
        deleted = e.response.status_code == 404
    print(f"[VERIFY]  item {key} absent from library: {deleted}")

    # ── 5. VERIFY PROV captured all three mutations ───────────────────────────
    recs = prov.query(key)
    acts = [r["activity"] for r in recs]
    print(f"\n[PROV]    {len(recs)} record(s) for {key}: {acts}")
    for r in recs:
        e = r["entity"]
        print(f"   - {r['activity']:12} ts={r['ts']}  "
              f"after_sha256={(e['after_sha256'] or '-')[:12]}  "
              f"reversible_blob={'yes' if e['after_blob'] or e['before_blob'] else 'no'}  "
              f"agent={r['agent']}/{r['tool_version']}")

    # ── Gate assertions ───────────────────────────────────────────────────────
    problems = []
    for need in ("create_item", "update_item", "delete_item"):
        if need not in acts:
            problems.append(f"no {need} PROV record")
    if not deleted:
        problems.append(f"item still present after delete -> MANUALLY DELETE item {key}")
    if problems:
        fail("; ".join(problems))

    print("\nPASS: gateway create->update->delete round-trip on the real library; structured "
          "envelope + version handling exercised; all three mutations recorded in PROV; throwaway "
          "item deleted (net-zero footprint).")


if __name__ == "__main__":
    main()
