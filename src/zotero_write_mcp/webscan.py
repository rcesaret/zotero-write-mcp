"""Read-only Web-API library pager (S5a) — shared by the dashboard, F5, and F7 tooling.

Pure GET traffic against api.zotero.org (never the local API, never a write) — honors ``Backoff`` /
``Retry-After`` per the platform-facts pin (parameter-registry.md). This is the same pager shape
``scripts/phase2_apply_reconciled.py`` already proved over the live 9,083-item library; generalized
here (off ``client.library_id`` instead of a module constant) so S5a's read-only tools can reuse it
without duplicating the paging loop.
"""
from __future__ import annotations

import time
from typing import Any


def web_items(client: Any, *, item_type: str = "-attachment", page: int = 100,
             include_trashed: bool = False) -> list:
    """Page through library items via ``GET /items``. Read-only.

    By default excludes trash (matches the platform default). ``include_trashed=True`` adds
    ``includeTrashed=1`` — needed by any orphan-file check comparing storage-dir keys against
    attachment items, since a TRASHED attachment still owns its on-disk file (not yet purged) and
    would otherwise be misreported as orphaned.
    """
    out: list = []
    start = 0
    while True:
        params = {"limit": page, "start": start, "itemType": item_type, "format": "json"}
        if include_trashed:
            params["includeTrashed"] = 1
        r = client._client.get(
            f"{client.web_url}/users/{client.library_id}/items",
            headers=client._web_headers,
            params=params,
        )
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        start += len(batch)
        if r.headers.get("Backoff"):
            time.sleep(int(r.headers["Backoff"]))
        if len(batch) < page:
            break
    return out
