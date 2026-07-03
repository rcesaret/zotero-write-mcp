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


def cluster_snapshot_from_dict(d: dict) -> "ClusterSnapshot":
    """Rebuild a :class:`ClusterSnapshot` from its ``to_json()`` form — i.e. the snapshot_cluster PROV
    before-image blob. Used by crash-recovery reconcile to roll back an orphaned commit from its snapshot
    (ADR-008: ``snapshot_id`` is the rollback index)."""
    def _att(a: dict) -> AttachmentSnap:
        a = dict(a)
        anns = [AnnotationSnap(**x) for x in (a.pop("annotations", []) or [])]
        return AttachmentSnap(annotations=anns, **a)
    return ClusterSnapshot(
        snapshot_id=d["snapshot_id"],
        master_key=d["master_key"],
        secondary_keys=list(d["secondary_keys"]),
        items={k: ItemSnap(**v) for k, v in d["items"].items()},
        notes=[NoteSnap(**n) for n in (d.get("notes") or [])],
        attachments=[_att(a) for a in (d.get("attachments") or [])],
    )


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


def build_cluster(
    reader: ClusterReader,
    master_key: str,
    secondary_keys: list,
    snapshot_id: Optional[str] = None,
) -> ClusterSnapshot:
    """Read a cluster's current state into a :class:`ClusterSnapshot` — **pure** (NO PROV write).

    Reads master + every secondary at their current version, all children (notes + attachments) and
    annotation grandchildren, per-attachment md5+filename, and pinned citekeys. Used by
    :func:`snapshot_cluster` (which then persists the result) AND by the Phase-2 live re-reads in
    ``merge_live`` (verify / M-4 re-assert / terminal verify), which must NOT append a PROV record on
    every internal re-read. Each child keeps its full ``json`` (incl. any ``deleted`` flag), so a
    commit-time reader that includes trashed children lets M-4 / the terminal verify detect a
    cascade-trashed child.
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

    return ClusterSnapshot(
        snapshot_id=sid, master_key=master_key, secondary_keys=list(secondary_keys),
        items=items, notes=notes, attachments=attachments,
    )


def snapshot_cluster(
    reader: ClusterReader,
    master_key: str,
    secondary_keys: list,
    *,
    prov: ProvenanceStore,
    snapshot_id: Optional[str] = None,
) -> ClusterSnapshot:
    """Capture + PERSIST the full before-image of a cluster (append-only PROV blob). Thin wrapper over
    :func:`build_cluster`; the ``snapshot_id`` is the rollback index and the audit ``wasDerivedFrom``
    (ADR-008)."""
    snap = build_cluster(reader, master_key, secondary_keys, snapshot_id)
    prov.record(
        activity="snapshot_cluster",
        item_key=master_key,
        before=snap.to_json(),
        snapshot_id=snap.snapshot_id,
        agent="merge-engine",
        tool_version=__version__,
        params={"master_key": master_key, "secondary_keys": list(secondary_keys),
                "n_notes": len(snap.notes), "n_attachments": len(snap.attachments)},
    )
    return snap


# ── verify_merge — the 11-check fail-closed gate (TC-3, §7, INV-COMP) ───────────

@dataclass
class CheckResult:
    number: int
    name: str
    passed: bool
    detail: str = ""


@dataclass
class IntegrityReport:
    """Result of the 11-check gate. ``passed`` is the AND of every check (fail-closed)."""
    passed: bool
    checks: list                      # CheckResult[]

    @property
    def failed(self) -> list:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {"pass": self.passed,
                "checks": [{"number": c.number, "name": c.name, "pass": c.passed, "detail": c.detail}
                           for c in self.checks]}


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _is_empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _rel_target_key(uri: Any) -> str:
    """Extract the trailing Zotero item key from a relation URI (``.../items/<KEY>``). Base- and
    library-agnostic, and uses EXACT key equality downstream — so secondary keys that are substrings
    of one another (``ABCD`` vs ``ABCD1``) or of an unrelated relation URI cannot false-positive.
    (Stage-E review MAJOR / red-team HOLE #2: the old ``k in v`` substring match was unsound.)"""
    return str(uri).rstrip("/").rsplit("/", 1)[-1]


# BBT-managed / identity fields enrichment + smart_fill must NEVER take from a duplicate (review #2): the
# survivor's pinned citation key IS its identity — overwriting it with a dup's key silently breaks citations.
_PROTECTED_FIELDS = frozenset({"citationKey"})


def _enriched_fields(snapshot: ClusterSnapshot, field_sources: Optional[dict]) -> dict:
    """Resolve the owner-approved per-field source selections ``{field: source_member_key}`` to concrete
    values, taken VERBATIM from the named member's snapshot (Phase-B field-level metadata reconciliation).
    Fabrication is impossible — every value is an existing member field — and projection + verify both call
    this so they agree on the expected enriched master. SKIPS: a protected identity field (citationKey,
    review #2), an unknown source, a field the source lacks, OR a source value that is EMPTY (review #1 —
    enrichment may ADD or IMPROVE, never blank a populated survivor field; clearing a field must be a
    separate explicit audited op, not a side effect of source selection)."""
    out: dict = {}
    for fld, src in (field_sources or {}).items():
        if fld in _PROTECTED_FIELDS:                                   # review #2: never reconcile identity fields
            continue
        src_item = snapshot.items.get(src)
        if src_item is not None and not _is_empty(src_item.fields.get(fld)):   # review #1: never blank a field
            out[fld] = src_item.fields[fld]
    return out


def _extra_add_tex_ids(extra_text: Optional[str], alias_keys: list) -> str:
    """Add ``alias_keys`` to a ``tex.ids:`` line in the Zotero ``extra`` field — Better BibTeX's mechanism
    for ALTERNATE citation keys, so citing a merged-away duplicate's key still resolves to the survivor.
    Preserves every other ``extra`` line and de-dups against any pre-existing tex.ids keys (idempotent)."""
    other: list = []
    existing: list = []
    for ln in (extra_text or "").splitlines():
        if ln.strip().lower().startswith("tex.ids:"):
            existing += [x.strip() for x in ln.split(":", 1)[1].split(",")]
        else:
            other.append(ln)
    merged = [k for k in dict.fromkeys(existing + list(alias_keys)) if k]
    if not merged:
        return extra_text or ""
    other = [ln for ln in other if ln.strip()]
    return "\n".join(other + [f"tex.ids: {', '.join(merged)}"])


def _tex_ids_of(extra_text: Optional[str]) -> list:
    """The citation-key aliases already declared in an item's ``extra`` via ``tex.ids:`` lines."""
    out: list = []
    for ln in str(extra_text or "").splitlines():
        if ln.strip().lower().startswith("tex.ids:"):
            out += [x.strip() for x in ln.split(":", 1)[1].split(",") if x.strip()]
    return out


def _citekey_from_extra(extra_text: Optional[str]) -> Optional[str]:
    """The Better BibTeX **pinned** citation key declared in an item's ``extra`` via a ``Citation Key:``
    line — BBT's mechanism for a manually pinned key, which (unlike the computed key) is stored in
    ``extra`` and therefore syncs to the Web API. Mirrors :func:`_tex_ids_of`'s line-parse. ``None`` when
    no such line is present (an unpinned item's key is computed, not stored here)."""
    for ln in str(extra_text or "").splitlines():
        if ln.strip().lower().startswith("citation key:"):
            val = ln.split(":", 1)[1].strip()
            if val:
                return val
    return None


def _alias_extra(snapshot: ClusterSnapshot, base_extra: Optional[str] = None) -> Optional[str]:
    """The survivor's ``extra`` with the trashed duplicates' BBT citekeys accumulated as ``tex.ids`` aliases
    (so a manuscript citing a duplicate's ``@citekey`` still resolves post-merge). Accumulates, per secondary,
    its pinned ``citationKey`` AND the aliases it ALREADY carries in its own extra ``tex.ids`` (review #3:
    transitive — a secondary that was itself a prior merge survivor keeps the inherited alias set). None when
    nothing to preserve."""
    alias_keys: list = []
    for s in snapshot.secondary_keys:
        sf = snapshot.items[s].fields
        ck = sf.get("citationKey")
        if ck:
            alias_keys.append(ck)
        alias_keys += _tex_ids_of(sf.get("extra"))
    if not alias_keys:
        return None
    base = base_extra if base_extra is not None else snapshot.items[snapshot.master_key].fields.get("extra")
    return _extra_add_tex_ids(base, alias_keys)


def _master_overrides(snapshot: ClusterSnapshot, field_sources: Optional[dict]) -> dict:
    """The full set of survivor scalar-field overrides a merge applies: the owner-approved field-level
    enrichment (``_enriched_fields``) PLUS citekey-alias accumulation (the duplicates' BBT citekeys preserved
    as ``tex.ids`` on the survivor's ``extra``). Projection, verify, AND the merge PATCH all derive the
    overrides from this single function, so they agree and the gate enforces EXACTLY this result."""
    out = _enriched_fields(snapshot, field_sources)
    alias = _alias_extra(snapshot, out.get("extra"))
    if alias is not None:
        out["extra"] = alias
    return out


def verify_merge(
    snapshot: ClusterSnapshot,
    observed: ClusterSnapshot,
    *,
    smart_fill: bool = False,
    field_sources: Optional[dict] = None,
) -> IntegrityReport:
    """The 11-check fail-closed gate. Compares the before-image ``snapshot`` to the post-merge
    (live or projected) ``observed`` cluster state; passes only if EVERY check passes (§7).

    All expectations are recomputed **independently from the snapshot** (never trusting the observed
    union); each check maps to a Stage-E C-1.x corruption. Pure function — no I/O. ``field_sources`` (Phase B)
    threads the owner-approved metadata reconciliation so check #3 expects the ENRICHED master (each field
    == its chosen source member's value) rather than bare survivor preservation.
    """
    checks: list = []
    m = snapshot.master_key
    sm = snapshot.items[m]
    obs_m = observed.items.get(m)
    sec_keys = list(snapshot.secondary_keys)
    cluster_items = [sm] + [snapshot.items[k] for k in sec_keys]

    def add(number, name, passed, detail=""):
        checks.append(CheckResult(number, name, bool(passed), detail))

    if obs_m is None:
        add(0, "master-present", False, f"master {m} absent from observed state")
        return IntegrityReport(False, checks)

    # 1 — item-type equality (master unchanged; every secondary == master type)
    bad_types = [k for k in sec_keys
                 if observed.items.get(k) is None or observed.items[k].item_type != obs_m.item_type]
    add(1, "item-type-equality",
        obs_m.item_type == sm.item_type and not bad_types,
        f"master {obs_m.item_type} vs snapshot {sm.item_type}; mismatched secondaries={bad_types}")

    # 2 — version-drift: secondaries UNTOUCHED between snapshot and pre-DELETE verify (a changed
    #     secondary version signals a concurrent external edit -> fail-closed). The master is excluded:
    #     it legitimately advances when PATCHed. NOTE (H-1 residual): in Phase-1 shadow there is no live
    #     PATCH, so master freshness is NOT enforced by this gate; a concurrent edit to the master in the
    #     merge window is closed at Phase-2 PATCH time via If-Unmodified-Since-Version, not here.
    drift = [k for k in sec_keys
             if observed.items.get(k) is None or observed.items[k].version != snapshot.items[k].version]
    add(2, "version-drift", not drift, f"secondaries with version drift={drift}")

    # 3 — master scalar-field preservation / approved enrichment, SYMMETRIC (REV1 M-HIGH-1): each
    #     non-empty EXPECTED master scalar (incl. creators) is byte-identical in observed (lower bound),
    #     AND observed introduces NO value the projection does not expect (upper bound — a deviant field
    #     ADDED to a snapshot-EMPTY slot by a concurrent edit or a rogue smart_fill is rejected; the old
    #     presence-only check skipped empty slots). EXPECTED = the snapshot master's fields, overlaid with
    #     (smart_fill) master-empty fields filled from a secondary and (Phase B) the owner-approved
    #     enrichment — recomputed independently from the snapshot (mirrors compute_merge_projection's
    #     field logic and the collections `==` upper bound), never trusting observed.
    overrides = _master_overrides(snapshot, field_sources)
    expected_fields = dict(sm.fields)
    if smart_fill:
        for _sk in sec_keys:
            for _fk, _fv in snapshot.items[_sk].fields.items():
                if _fk not in _PROTECTED_FIELDS and _is_empty(expected_fields.get(_fk)) and not _is_empty(_fv):
                    expected_fields[_fk] = _fv
    expected_fields.update(overrides)
    changed, added = [], []
    for k, v in expected_fields.items():
        if _is_empty(v) and k not in overrides:        # expected-empty, non-override survivor slot...
            if not _is_empty(obs_m.fields.get(k)):     # ...must STAY empty; a value here is a deviant add
                added.append(k)
            continue
        if obs_m.fields.get(k) != v:
            changed.append(k)
    for k, v in obs_m.fields.items():                  # a key present in observed but not in the projection
        if k not in expected_fields and not _is_empty(v):
            added.append(k)
    add(3, "master-scalar-preservation", not changed and not added,
        f"master fields deviate from approved survivor: changed={changed}; added={sorted(set(added))}")

    # 4 — collections EQUALITY vs independently-recomputed union (==, not superset)
    expected_cols = set()
    for it in cluster_items:
        expected_cols |= set(it.collections)
    obs_cols = set(obs_m.collections)
    add(4, "collections-equality", obs_cols == expected_cols,
        f"observed={sorted(obs_cols)} expected={sorted(expected_cols)}")

    # 5 — tags tuple-superset on (tag, type); no type flips (a flip drops the original tuple → caught)
    expected_tags = set()
    for it in cluster_items:
        expected_tags |= {tuple(t) for t in it.tags}
    obs_tags = {tuple(t) for t in obs_m.tags}
    lost_tags = expected_tags - obs_tags                      # any snapshot (tag,type) dropped
    expected_types: dict = {}
    for (t, ty) in expected_tags:
        expected_types.setdefault(t, set()).add(ty)
    # a (tag, type) whose tag is known but whose type was in NO source = a type flip (incl. the
    # additive flip that keeps the original tuple; red-team HOLE #1).
    flipped_tags = [(t, ty) for (t, ty) in obs_tags
                    if t in expected_types and ty not in expected_types[t]]
    add(5, "tags-tuple-superset", not lost_tags and not flipped_tags,
        f"lost={sorted(lost_tags)}; type-flips={sorted(flipped_tags)}")

    # 6 — relations superset (full dict) incl. dc:replaces → each secondary, SYMMETRIC (REV1 M-HIGH-3):
    #     observed ⊇ the independently-recomputed union (lower bound) AND observed ⊆ that union plus the
    #     cluster's own dc:replaces→secondaries (upper bound — a deviant ADDED relation, e.g. a dc:replaces
    #     to a victim OUTSIDE the cluster or an injected owl:sameAs, is rejected, symmetric to the
    #     collections `==` check). dc:replaces is compared by EXACT target KEY (library-base-agnostic).
    expected_rel: dict = {}
    for it in cluster_items:
        for pred, vals in it.relations.items():
            expected_rel.setdefault(pred, set()).update(_as_list(vals))
    rel_missing = []
    for pred, vals in expected_rel.items():
        if not vals <= set(_as_list(obs_m.relations.get(pred))):
            rel_missing.append(pred)
    dc_target_keys = {_rel_target_key(x) for x in _as_list(obs_m.relations.get("dc:replaces"))}
    dc_missing = [k for k in sec_keys if k not in dc_target_keys]   # EXACT key membership, not substring
    # Upper bound: reject any observed relation the merge should NOT have introduced. Allowed dc:replaces
    # targets (by key) = any pre-existing (from a member) + this cluster's secondaries; every other
    # predicate's observed targets must be a subset of the recomputed union.
    allowed_dc_keys = {_rel_target_key(x) for x in expected_rel.get("dc:replaces", set())} | set(sec_keys)
    rel_extra = []
    for pred, vals in obs_m.relations.items():
        obs_vals = set(_as_list(vals))
        if pred == "dc:replaces":
            deviant = sorted(x for x in obs_vals if _rel_target_key(x) not in allowed_dc_keys)
        else:
            deviant = sorted(obs_vals - expected_rel.get(pred, set()))
        if deviant:
            rel_extra.append((pred, deviant))
    add(6, "relations-superset", not rel_missing and not dc_missing and not rel_extra,
        f"missing predicates={rel_missing}; dc:replaces missing secondaries={dc_missing}; "
        f"deviant-added={rel_extra}")

    # 7 — child completeness by presence: every snapshot child live & parented to master
    obs_parent = {n.key: n.parent_key for n in observed.notes}
    obs_parent.update({a.key: a.parent_key for a in observed.attachments})
    orphaned = [c.key for c in (snapshot.notes + snapshot.attachments) if obs_parent.get(c.key) != m]
    add(7, "child-completeness", not orphaned, f"missing/misparented children={orphaned}")

    # 8 — count parity: notes AND attachments, as SEPARATE invariants
    add(8, "note-count-parity", len(observed.notes) == len(snapshot.notes),
        f"observed={len(observed.notes)} snapshot={len(snapshot.notes)}")
    add(8, "attachment-count-parity", len(observed.attachments) == len(snapshot.attachments),
        f"observed={len(observed.attachments)} snapshot={len(snapshot.attachments)}")

    # 9 — annotation parity per attachment
    snap_ann = {a.key: len(a.annotations) for a in snapshot.attachments}
    obs_ann = {a.key: len(a.annotations) for a in observed.attachments}
    ann_bad = [k for k, n in snap_ann.items() if obs_ann.get(k) != n]
    add(9, "annotation-parity", not ann_bad, f"attachments with annotation drift={ann_bad}")

    # 10 — attachment storage integrity: md5 AND filename == snapshot, per attachment
    snap_st = {a.key: (a.md5, a.filename) for a in snapshot.attachments}
    obs_st = {a.key: (a.md5, a.filename) for a in observed.attachments}
    st_bad = [k for k, v in snap_st.items() if obs_st.get(k) != v]
    add(10, "attachment-storage-integrity", not st_bad, f"md5/filename drift={st_bad}")

    # 11 — citekey preservation: master keeps its pinned BBT citekey (no merge-introduced change).
    #      ("No new collision" beyond the master's own key needs a library-wide BBT scan — Phase 2.)
    add(11, "citekey-preservation", obs_m.citekey == sm.citekey,
        f"observed={obs_m.citekey!r} snapshot={sm.citekey!r}")

    passed = all(c.passed for c in checks)
    return IntegrityReport(passed, checks)


# ── compute_merge_projection — the shadow "golden" post-merge state (no writes) ─

def compute_merge_projection(
    snapshot: ClusterSnapshot,
    *,
    smart_fill: bool = False,
    library_base: str = "http://zotero.org/users/0/items",
    field_sources: Optional[dict] = None,
) -> ClusterSnapshot:
    """Compute what a merge WOULD produce, purely from the snapshot — NO writes (this is the heart
    of shadow mode). The master gets the unioned collections/tags/relations + ``dc:replaces``→each
    secondary; all children reparent to the master; secondaries stay (pre-DELETE). ``verify_merge``
    against this projection should pass on every check; the injection suite corrupts it one way at a
    time and asserts the matching check fails.
    """
    m = snapshot.master_key
    sm = snapshot.items[m]
    sec = [snapshot.items[k] for k in snapshot.secondary_keys]
    members = [sm] + sec

    cols = list(dict.fromkeys(c for it in members for c in it.collections))
    tags = list(dict.fromkeys(tuple(t) for it in members for t in it.tags))

    rel: dict = {}
    for it in members:
        for pred, vals in it.relations.items():
            bucket = rel.setdefault(pred, [])
            for x in _as_list(vals):
                if x not in bucket:
                    bucket.append(x)
    dc = rel.setdefault("dc:replaces", [])
    for k in snapshot.secondary_keys:
        uri = f"{library_base}/{k}"
        if uri not in dc:
            dc.append(uri)

    fields = dict(sm.fields)
    if smart_fill:
        for it in sec:
            for k, v in it.fields.items():
                if k not in _PROTECTED_FIELDS and _is_empty(fields.get(k)) and not _is_empty(v):
                    fields[k] = v
    fields.update(_master_overrides(snapshot, field_sources))   # Phase B: enrichment + citekey-alias accumulation

    proj_master = ItemSnap(
        key=m, version=sm.version + 1, item_type=sm.item_type,
        collections=cols, tags=tags, relations=rel, citekey=sm.citekey,
        fields=fields, json={**sm.json, "collections": cols, "relations": rel, **fields},
        sha256="",
    )
    items = {m: proj_master}
    for k in snapshot.secondary_keys:
        items[k] = snapshot.items[k]   # secondaries unchanged (deleted only at Phase-2 commit)

    notes = [NoteSnap(key=n.key, version=n.version + (0 if n.parent_key == m else 1),
                      parent_key=m, json=n.json) for n in snapshot.notes]
    atts = [AttachmentSnap(key=a.key, version=a.version + (0 if a.parent_key == m else 1),
                           parent_key=m, md5=a.md5, filename=a.filename,
                           content_type=a.content_type, json=a.json, annotations=list(a.annotations))
            for a in snapshot.attachments]

    return ClusterSnapshot(
        snapshot_id=f"{snapshot.snapshot_id}-proj", master_key=m,
        secondary_keys=list(snapshot.secondary_keys), items=items, notes=notes, attachments=atts,
    )


# ── rollback_merge — undo any of 3 partial states (TC-5; Stage-E C-2) ───────────

@dataclass
class RestoreReport:
    """Outcome of a rollback. ``state`` ∈ {"a","b","c"}; ``operations`` = the restore ops that SUCCEEDED;
    ``failures`` = restore ops that threw. ``ok`` is False when any op failed — the caller MUST escalate
    (a partial rollback leaves a half-restored library that needs human recovery, review R-3)."""
    state: str
    operations: list                  # [{op, key, ...}] — succeeded
    ok: bool = True
    failures: list = field(default_factory=list)   # [{op, key, error}]


class RollbackGateway(Protocol):
    """The subset of WriteGateway rollback needs (injectable; tests record calls)."""
    def update_item(self, library_id, item_key, data, version, *, library_type=...): ...
    def create_items(self, library_id, objects, *, library_type=...): ...


def _zotero_tags(tags: list) -> list:
    return [{"tag": t, "type": ty} for (t, ty) in (tuple(x) for x in tags)]


def _is_trashed(item: ItemSnap) -> bool:
    """True if the observed item carries a truthy Zotero ``deleted`` flag. READER CONTRACT: a
    trashed-but-present secondary MUST appear in ``observed.items`` with ``deleted`` truthy so rollback
    chooses un-trash (recoverable) over recreate (lossy); a reader that omits trashed items entirely
    would mis-route state (c) to the recreate branch."""
    return item.json.get("deleted") in (1, "1", True)


def _master_differs(sm: ItemSnap, om: ItemSnap) -> bool:
    return (set(sm.collections) != set(om.collections)
            or {tuple(t) for t in sm.tags} != {tuple(t) for t in om.tags}
            or sm.relations != om.relations
            or sm.fields != om.fields)


def rollback_merge(
    snapshot: ClusterSnapshot,
    observed: ClusterSnapshot,
    gateway: RollbackGateway,
    *,
    library_id: int,
    library_type: str = "user",
) -> RestoreReport:
    """Undo a merge from the snapshot, handling all 3 partial states (Stage-E C-2):

    * **(a) nothing written** → no-op.
    * **(b) PATCHed-not-deleted** → revert master collections/tags/relations/fields to snapshot AND
      re-parent children to their original parents.
    * **(c) partial DELETE** → restore the trashed/absent secondaries (un-trash via ``deleted:0``, or
      recreate from the snapshot if hard-gone), THEN apply (b). Never relies on un-trash alone.

    Versions for each PATCH come from the OBSERVED (current) state. Pure orchestration over the
    injected gateway — deterministic and unit-testable with a fake gateway.
    """
    m = snapshot.master_key
    sm = snapshot.items[m]
    obs_m = observed.items.get(m)
    ops: list = []

    trashed, absent = [], []
    for k in snapshot.secondary_keys:
        oi = observed.items.get(k)
        if oi is None:
            absent.append(k)
        elif _is_trashed(oi):
            trashed.append(k)

    master_changed = (obs_m is None) or _master_differs(sm, obs_m)
    obs_children = {c.key: c for c in (observed.notes + observed.attachments)}
    reparented = [c for c in (snapshot.notes + snapshot.attachments)
                  if c.key in obs_children and obs_children[c.key].parent_key != c.parent_key]

    if not trashed and not absent and not master_changed and not reparented:
        return RestoreReport(state="a", operations=[], ok=True)

    state = "c" if (trashed or absent) else "b"
    failures: list = []

    def _do(op: dict, fn) -> None:
        # R-3: each restore op is independently fail-safe — a thrown gateway error (412/5xx) is recorded,
        # not propagated, so a partial rollback returns ok=False (loudly escalated) instead of an uncaught
        # exception that hides a half-restored library.
        try:
            fn()
            ops.append(op)
        except Exception as e:
            failures.append({**op, "error": f"{type(e).__name__}: {e}"})

    # (c) restore secondaries FIRST (so children have a parent to return to)
    for k in trashed:
        _do({"op": "untrash", "key": k},
            lambda k=k: gateway.update_item(library_id, k, {"deleted": 0}, observed.items[k].version,
                                            library_type=library_type))
    for k in absent:
        # PHASE-2-INCOMPLETE: re-creating a hard-gone secondary needs version:0 (valid only if PURGED, not
        # trashed). Under "trash, never purge" a secondary is trashed and takes the un-trash path above;
        # this absent->recreate branch is a defensive stub to harden before live rollback.
        _do({"op": "recreate", "key": k},
            lambda k=k: gateway.create_items(library_id, [snapshot.items[k].json], library_type=library_type))

    # (b) revert master scalar/array fields, then re-parent children to their original parents
    if obs_m is not None and master_changed:
        revert = {**sm.fields, "collections": sm.collections,
                  "tags": _zotero_tags(sm.tags), "relations": sm.relations}
        # Clear any scalar field the merge ADDED to the master (present in observed, absent in the snapshot
        # — e.g. a Phase-B enrichment or smart_fill field like publisher) so the revert fully restores the
        # survivor: a PATCH leaves omitted fields unchanged, so an added field needs an explicit "".
        for fk in obs_m.fields:
            if fk not in revert:
                revert[fk] = ""
        _do({"op": "revert-master", "key": m},
            lambda: gateway.update_item(library_id, m, revert, obs_m.version, library_type=library_type))

    for c in reparented:
        oc = obs_children[c.key]
        _do({"op": "reparent", "key": c.key, "to": c.parent_key},
            lambda c=c, oc=oc: gateway.update_item(library_id, c.key, {"parentItem": c.parent_key},
                                                   oc.version, library_type=library_type))

    return RestoreReport(state=state, operations=ops, ok=(not failures), failures=failures)


# ── shadow_merge — compute + verify + log, NO commit (TC-3 shadow; ADR-004) ─────

@dataclass
class ShadowReport:
    snapshot_id: str
    passed: bool
    integrity: IntegrityReport
    projection: ClusterSnapshot


def shadow_merge(
    reader: ClusterReader,
    master_key: str,
    dup_keys: list,
    *,
    prov: ProvenanceStore,
    smart_fill: bool = False,
    observed: Optional[ClusterSnapshot] = None,
    library_base: str = "http://zotero.org/users/0/items",
    field_sources: Optional[dict] = None,
) -> ShadowReport:
    """Shadow mode: ``snapshot → compute projection → verify → LOG``. **No writes** — this function
    takes no gateway and structurally cannot commit (ADR-004 shadow phase: the isolation mechanism that
    replaces a test library, decision Q8).

    By default verify runs against the computed projection (always self-consistent — this exercises the
    pipeline + PROV observability). Pass ``observed`` (a re-read of the live post-merge state) to verify
    real discrepancies — that is how Phase 2 wires shadow to the live ``merge_cluster`` output, and how
    the live dc:replaces smoke verifies a real merge.
    """
    snap = snapshot_cluster(reader, master_key, dup_keys, prov=prov)
    projection = compute_merge_projection(snap, smart_fill=smart_fill, library_base=library_base,
                                          field_sources=field_sources)
    state = observed if observed is not None else projection
    report = verify_merge(snap, state, smart_fill=smart_fill, field_sources=field_sources)
    prov.record(
        activity="shadow_merge", item_key=master_key, snapshot_id=snap.snapshot_id,
        agent="merge-engine", tool_version=__version__,
        params={"pass": report.passed, "n_checks": len(report.checks),
                "failed": [c.name for c in report.failed],
                "against": "observed" if observed is not None else "projection",
                "secondary_keys": list(dup_keys), "smart_fill": smart_fill},
    )
    return ShadowReport(snapshot_id=snap.snapshot_id, passed=report.passed,
                        integrity=report, projection=projection)
