"""Live merge execution layer (Phase 2): merge_cluster (PATCH) + commit_merge (verify-gated trash).

Builds ON the pure shadow spine in merge.py (snapshot_cluster, compute_merge_projection, verify_merge,
rollback_merge). `merge_cluster` does the REVERSIBLE PATCH phase (reparent children + union master, NO
delete — undoable via rollback_merge state b). `commit_merge` (added next) does the verify-gated,
enable-token-and-observability-gated, fail-closed TRASH.

TRASH-NOT-PURGE: secondaries are trashed via PATCH ``{"deleted":1}``, NEVER ``delete_items`` (which the
Zotero Web API PURGES — confirmed Phase 0: DELETE -> GET 404). Unit-testable offline against a fake
reader + fake gateway; no live writes occur in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from zotero_write_mcp.gateway import library_prefix
from zotero_write_mcp.merge import (
    ClusterSnapshot, compute_merge_projection, _is_empty, _unwrap, _zotero_tags,
)


def library_item_base(library_type: str, library_id: int) -> str:
    """The Zotero item-URI base for ``dc:replaces`` values, e.g. http://zotero.org/users/<id>/items."""
    return f"http://zotero.org{library_prefix(library_type, library_id)}/items"


@dataclass
class MergePlan:
    """Outcome of the PATCH phase. ``drifted`` True => no writes issued (caller must re-snapshot)."""
    drifted: bool
    drift_keys: list = field(default_factory=list)
    patches: list = field(default_factory=list)        # [{op, key, version, ...}]
    master_version: Optional[int] = None


def merge_cluster(
    snapshot: ClusterSnapshot,
    reader: Any,
    gateway: Any,
    *,
    library_id: int,
    smart_fill: bool = False,
    library_type: str = "user",
) -> MergePlan:
    """PATCH phase of a merge: PATCH the master with the unioned projection (collections/tags/relations +
    dc:replaces) and re-parent every child to the master. **NO delete** — fully reversible via
    ``rollback_merge`` (state b). Aborts with NO writes on version drift (a cluster item changed since the
    snapshot), so the caller re-snapshots rather than merging stale data (ADR-006)."""
    m = snapshot.master_key

    # 1. Drift check — re-read master + secondaries; current versions must equal the snapshot.
    drift: list = []
    fresh_ver: dict = {}
    fresh_data: dict = {}
    for k in [m, *snapshot.secondary_keys]:
        _, ver, data = _unwrap(reader.get_item(k))
        fresh_ver[k], fresh_data[k] = ver, data
        if ver != snapshot.items[k].version:
            drift.append(k)
    if drift:
        return MergePlan(drifted=True, drift_keys=drift)

    # 2. Projection (golden target) -> master PATCH body (union of collections/tags/relations + dc:replaces).
    base = library_item_base(library_type, library_id)
    proj = compute_merge_projection(snapshot, smart_fill=smart_fill, library_base=base)
    pm = proj.items[m]
    master_data: dict = {
        "collections": pm.collections,
        "tags": _zotero_tags(pm.tags),
        "relations": pm.relations,
    }
    if smart_fill:
        # M-2: fill a master field only if it is empty in BOTH the snapshot AND the LIVE master, so a
        # value populated after the snapshot is never overwritten (version-drift abort backs this up).
        snap_fields = snapshot.items[m].fields
        live = fresh_data[m]
        for k, v in pm.fields.items():
            if _is_empty(snap_fields.get(k)) and not _is_empty(v) and _is_empty(live.get(k)):
                master_data[k] = v

    # 3. Execute: PATCH master, then re-parent every child whose original parent is a secondary.
    patches: list = []
    gateway.update_item(library_id, m, master_data, fresh_ver[m], library_type=library_type)
    patches.append({"op": "patch-master", "key": m, "version": fresh_ver[m]})
    for c in (snapshot.notes + snapshot.attachments):
        if c.parent_key != m:
            gateway.update_item(library_id, c.key, {"parentItem": m}, c.version, library_type=library_type)
            patches.append({"op": "reparent", "key": c.key, "version": c.version, "to": m})

    return MergePlan(drifted=False, patches=patches, master_version=fresh_ver[m])
