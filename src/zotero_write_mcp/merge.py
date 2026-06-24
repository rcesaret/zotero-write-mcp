"""Merge transaction engine — snapshot / verify / rollback / shadow (Phase 1, shadow-only).

The *safety spine* for full-auto merge. Phase 1 builds, in dependency order:
  * ``snapshot_cluster``         — capture an immutable before-image of a duplicate cluster.
  * ``compute_merge_projection`` — compute what a merge WOULD do (shadow; no writes).        [P1-shadow]
  * ``verify_merge``             — the 11-check fail-closed gate (pure fn over snapshot+state). [P1-verify-11]
  * ``rollback_merge``           — undo any of 3 partial states.                               [P1-rollback-3]
  * ``shadow_merge``             — snapshot → project → verify → log; NO commit.               [P1-shadow]

merge_cluster (live PATCH) and commit_merge (DELETE) are **Phase 2** — not built here. Safety is
computational: ``verify_merge`` decides structural validity; the model never judges its own merge
(ADR-005/006; INV-COMP/INV-VERIFY). Reads go through an injectable :class:`ClusterReader` (tests use a
fake; the live reader wraps the read API + Better BibTeX JSON-RPC). Snapshots persist to the
:class:`~zotero_write_mcp.provenance.ProvenanceStore` — ``snapshot_id`` is the rollback index AND the
audit ``wasDerivedFrom`` (ADR-008: "the audit trail IS the rollback index").

This module is intentionally dependency-light (stdlib + provenance) so every function is unit-testable
offline with a fake reader / fake gateway — no network, no live library.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol

from zotero_write_mcp import __version__
from zotero_write_mcp.provenance import ProvenanceStore, json_sha256

# Master 'data' keys NOT subject to scalar-field preservation (check #3): the array fields are
# verified independently (collections #4 / tags #5 / relations #6); the rest are volatile/identity.
_NON_SCALAR_FIELDS = frozenset(
    {"collections", "tags", "relations", "version", "dateModified", "dateAdded", "key", "mtime"}
)


# ── Data model (the before-image) ──────────────────────────────────────────────

@dataclass
class AnnotationSnap:
    key: str
    version: int
    json: dict


@dataclass
class NoteSnap:
    key: str
    version: int
    parent_key: str          # original parent (for rollback re-parenting)
    json: dict


@dataclass
class AttachmentSnap:
    key: str
    version: int
    parent_key: str          # original parent (for rollback re-parenting)
    md5: Optional[str]       # storage-integrity source of truth (check #10)
    filename: Optional[str]
    content_type: Optional[str]
    json: dict
    annotations: list = field(default_factory=list)   # AnnotationSnap[] (grandchildren; check #9)


@dataclass
class ItemSnap:
    """A single cluster member (master or secondary) at snapshot time."""
    key: str
    version: int
    item_type: str
    collections: list                      # collection keys (check #4)
    tags: list                             # (tag, type) pairs (check #5)
    relations: dict                        # full relations dict (check #6)
    citekey: Optional[str]                 # pinned BBT citekey (check #11)
    fields: dict                           # scalar editable fields (check #3)
    json: dict                             # full 'data'
    sha256: str


@dataclass
class ClusterSnapshot:
    """Immutable before-image of a duplicate cluster (master + secondaries + children + grandchildren)."""
    snapshot_id: str
    master_key: str
    secondary_keys: list
    items: dict                            # key -> ItemSnap (master + secondaries)
    notes: list = field(default_factory=list)          # NoteSnap[] across the whole cluster
    attachments: list = field(default_factory=list)    # AttachmentSnap[] across the whole cluster

    def children_of(self, parent_key: str) -> list:
        """All snapshot children (notes + attachments) whose original parent is ``parent_key``."""
        return ([n for n in self.notes if n.parent_key == parent_key]
                + [a for a in self.attachments if a.parent_key == parent_key])

    def to_json(self) -> dict:
        return asdict(self)


# ── Reader abstraction (injectable; live reader wraps read API + BBT JSON-RPC) ──

class ClusterReader(Protocol):
    """Read-only access to live item state. The live impl wraps the read server / web GET + BBT
    JSON-RPC; tests inject a fake. All reads should be **version-accurate** (web GET) so the snapshot
    and the post-merge verify compare like-for-like (the version-drift check depends on it)."""

    def get_item(self, key: str) -> dict: ...
    def get_children(self, key: str) -> list: ...
    def get_annotations(self, attachment_key: str) -> list: ...
    def get_citekey(self, key: str) -> Optional[str]: ...


def _unwrap(raw: dict) -> tuple[str, int, dict]:
    """Normalize a Zotero item payload to (key, version, data). Accepts the web-API envelope
    ``{key, version, data:{...}}`` or a flat data dict."""
    data = raw.get("data", raw)
    key = raw.get("key") or data.get("key")
    version = raw.get("version", data.get("version"))
    return key, int(version) if version is not None else 0, data


def _item_snap(reader: ClusterReader, raw: dict) -> ItemSnap:
    key, version, data = _unwrap(raw)
    tags = [(t.get("tag"), int(t.get("type", 0))) for t in data.get("tags", []) or []]
    return ItemSnap(
        key=key,
        version=version,
        item_type=data.get("itemType"),
        collections=list(data.get("collections", []) or []),
        tags=tags,
        relations=dict(data.get("relations", {}) or {}),
        citekey=_safe_citekey(reader, key),
        fields={k: v for k, v in data.items() if k not in _NON_SCALAR_FIELDS},
        json=dict(data),
        sha256=json_sha256(data),
    )


def _safe_citekey(reader: ClusterReader, key: str) -> Optional[str]:
    try:
        return reader.get_citekey(key)
    except Exception:
        return None  # citekey read is best-effort; #11 only requires master's, fetched again at verify


def snapshot_cluster(
    reader: ClusterReader,
    master_key: str,
    secondary_keys: list,
    *,
    prov: ProvenanceStore,
    snapshot_id: Optional[str] = None,
) -> ClusterSnapshot:
    """Capture the full before-image of a cluster and persist it (append-only) to the PROV store.

    Reads master + every secondary at their **current version**, plus all children (notes +
    attachments) and annotation grandchildren under each attachment, plus per-attachment md5+filename
    and pinned citekeys. Returns the :class:`ClusterSnapshot`; its ``snapshot_id`` is the rollback
    index and the audit ``wasDerivedFrom`` (ADR-008).
    """
    sid = snapshot_id or uuid.uuid4().hex
    items: dict = {}
    notes: list = []
    attachments: list = []

    for parent_key in [master_key, *secondary_keys]:
        items[parent_key] = _item_snap(reader, reader.get_item(parent_key))
        for child in reader.get_children(parent_key):
            ck, cv, cdata = _unwrap(child)
            ctype = cdata.get("itemType")
            if ctype == "note":
                notes.append(NoteSnap(key=ck, version=cv, parent_key=parent_key, json=dict(cdata)))
            elif ctype == "attachment":
                anns = [
                    AnnotationSnap(key=_unwrap(a)[0], version=_unwrap(a)[1], json=dict(_unwrap(a)[2]))
                    for a in reader.get_annotations(ck)
                ]
                attachments.append(AttachmentSnap(
                    key=ck, version=cv, parent_key=parent_key,
                    md5=cdata.get("md5"), filename=cdata.get("filename"),
                    content_type=cdata.get("contentType"),
                    json=dict(cdata), annotations=anns,
                ))
            # other child types (rare) are captured by neither bucket by design; extend if needed.

    snap = ClusterSnapshot(
        snapshot_id=sid, master_key=master_key, secondary_keys=list(secondary_keys),
        items=items, notes=notes, attachments=attachments,
    )

    # Persist the before-image: PROV record + reversible blob (snapshot_id = wasDerivedFrom = rollback idx).
    prov.record(
        activity="snapshot_cluster",
        item_key=master_key,
        before=snap.to_json(),
        snapshot_id=sid,
        agent="merge-engine",
        tool_version=__version__,
        params={"master_key": master_key, "secondary_keys": list(secondary_keys),
                "n_notes": len(notes), "n_attachments": len(attachments)},
    )
    return snap
