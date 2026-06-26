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

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from zotero_write_mcp import __version__
from zotero_write_mcp.gateway import ConcurrencyConflictError, library_prefix
from zotero_write_mcp.merge import (
    ClusterSnapshot, RestoreReport, build_cluster, cluster_snapshot_from_dict, compute_merge_projection,
    rollback_merge, verify_merge, _as_list, _is_empty, _is_trashed, _unwrap, _zotero_tags,
)
from zotero_write_mcp.observability import observability_is_fresh
from zotero_write_mcp.provenance import ProvenanceStore


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
    field_sources: Optional[dict] = None,
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
    # MC-1: children must also be unchanged. A child-only edit bumps the child's version but NOT the
    # parent's, so the parent-only check above cannot see it — re-read each cluster parent's children.
    fresh_child_ver: dict = {}
    for parent in [m, *snapshot.secondary_keys]:
        for child in reader.get_children(parent):
            ck, cver, _ = _unwrap(child)
            fresh_child_ver[ck] = cver
    for c in (snapshot.notes + snapshot.attachments):
        cur = fresh_child_ver.get(c.key)
        if cur is None or cur != c.version:          # edited, re-versioned, or externally re-parented
            drift.append(c.key)
    if drift:
        return MergePlan(drifted=True, drift_keys=drift)

    # 2. Projection (golden target) -> master PATCH body (union of collections/tags/relations + dc:replaces).
    base = library_item_base(library_type, library_id)
    proj = compute_merge_projection(snapshot, smart_fill=smart_fill, library_base=base,
                                    field_sources=field_sources)
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
    # Phase B: apply the owner-approved field-level enrichment (each reconciled field <- its chosen source
    # member's value, taken from the projection). The drift check above + retry_on_412=False guarantee the
    # live master is unchanged since the snapshot, so overwriting its scalar fields never clobbers a
    # concurrent edit; verify_merge check #3 (also given field_sources) confirms the result is EXACTLY this.
    for fld in (field_sources or {}):
        if fld in pm.fields:
            master_data[fld] = pm.fields[fld]

    # 3. Execute fail-closed (retry_on_412=False): a 412 = a concurrent edit landed AFTER the drift
    #    check -> abort (report the partial so the caller can rollback + re-snapshot), never blind-re-apply
    #    a stale body (review M1/F6/MC-2). Re-parent with the FRESH child version (MC-1).
    patches: list = []
    try:
        gateway.update_item(library_id, m, master_data, fresh_ver[m],
                            library_type=library_type, retry_on_412=False)
        patches.append({"op": "patch-master", "key": m, "version": fresh_ver[m]})
        for c in (snapshot.notes + snapshot.attachments):
            if c.parent_key != m:
                gateway.update_item(library_id, c.key, {"parentItem": m}, fresh_child_ver[c.key],
                                    library_type=library_type, retry_on_412=False)
                patches.append({"op": "reparent", "key": c.key, "version": fresh_child_ver[c.key], "to": m})
    except ConcurrencyConflictError:
        return MergePlan(drifted=True, drift_keys=["<concurrent-edit-after-drift-check>"],
                         patches=patches, master_version=fresh_ver[m])

    return MergePlan(drifted=False, patches=patches, master_version=fresh_ver[m])


# ── commit_merge — verify-gated, enable-token + observability-gated, fail-closed trash ──────────────

ENABLE_ENV = "ZOT_MERGE_LIVE_ENABLED"
ENABLE_TOKEN = "I-UNDERSTAND-LIVE-MERGE"     # the env var must equal this; NEVER an LLM-writable tool param
DEFAULT_CEILING = 10                          # max secondaries per commit (H-1: no unbounded sequential trash)
DEFAULT_FRESHNESS_WINDOW = 48 * 3600          # observability must be this fresh (C-2)


def live_merge_enabled() -> bool:
    """C-1: the out-of-band owner enable gate. True only when the environment carries the exact token.
    Never a tool parameter — the agent cannot enable live merge by passing an argument."""
    return os.environ.get(ENABLE_ENV) == ENABLE_TOKEN


@dataclass
class TerminalReport:
    passed: bool
    failed: list = field(default_factory=list)


@dataclass
class CommitResult:
    mode: str                                  # "committed" | "shadow" | "blocked" | "rolled_back"
    reason: str = ""
    verify_passed: Optional[bool] = None
    trashed: list = field(default_factory=list)
    rollback: Optional[RestoreReport] = None
    intent_prov_id: Optional[str] = None


def _reassert_children(snapshot, post, gateway, library_id, library_type) -> bool:
    """M-4: after the trash, every snapshot child must be live + parented to the master. A child that was
    cascade-trashed (or re-orphaned) is re-asserted via PATCH {deleted:0, parentItem:master}. Returns
    False (→ rollback, C-4) on a vanished child or any re-assert failure."""
    m = snapshot.master_key
    post_children = {c.key: c for c in (post.notes + post.attachments)}
    for c in (snapshot.notes + snapshot.attachments):
        pc = post_children.get(c.key)
        if pc is None:
            return False
        if _is_trashed(pc) or pc.parent_key != m:
            try:
                gateway.update_item(library_id, c.key, {"deleted": 0, "parentItem": m}, pc.version,
                                    library_type=library_type)
            except Exception:
                return False
    return True


def _terminal_verify(snapshot, final, sec_keys, *, library_base, smart_fill=False, field_sources=None) -> TerminalReport:
    """C-5: scoped re-verify of the FINAL committed state (the full 11-check verify cannot run here — the
    secondaries' versions legitimately changed via the trash). Asserts: every child live + parented to
    master; annotation parity + storage integrity per attachment; every secondary present AND trashed;
    AND (F5) the MASTER still matches the merge projection — a concurrent external edit to the master
    during the trash window passes the child/secondary checks but is a silent lossy merge."""
    m = snapshot.master_key
    failed: list = []
    final_children = {c.key: c for c in (final.notes + final.attachments)}
    for c in (snapshot.notes + snapshot.attachments):
        fc = final_children.get(c.key)
        if fc is None or fc.parent_key != m or _is_trashed(fc):
            failed.append(f"child:{c.key}")
    final_att = {a.key: a for a in final.attachments}
    for a in snapshot.attachments:
        fa = final_att.get(a.key)
        if fa is None:
            failed.append(f"att-missing:{a.key}")
            continue
        if len(fa.annotations) != len(a.annotations):
            failed.append(f"annot:{a.key}")
        if (fa.md5, fa.filename) != (a.md5, a.filename):
            failed.append(f"storage:{a.key}")
    for k in sec_keys:
        si = final.items.get(k)
        if si is None or not _is_trashed(si):
            failed.append(f"secondary-not-trashed:{k}")
    # F5: the master must still match the merge projection (collections/tags/relations + scalar fields).
    proj_m = compute_merge_projection(snapshot, smart_fill=smart_fill, library_base=library_base,
                                      field_sources=field_sources).items[m]
    fm = final.items.get(m)
    if fm is None:
        failed.append("master-absent")
    else:
        if set(fm.collections) != set(proj_m.collections):
            failed.append("master-collections")
        if {tuple(t) for t in proj_m.tags} - {tuple(t) for t in fm.tags}:
            failed.append("master-tags")
        for pred, vals in proj_m.relations.items():
            if not set(_as_list(vals)) <= set(_as_list(fm.relations.get(pred))):
                failed.append("master-relations")
                break
        for fk, v in proj_m.fields.items():       # F5 expects the ENRICHED master (== projection), Phase B
            if not _is_empty(v) and fm.fields.get(fk) != v:
                failed.append(f"master-field:{fk}")
                break
    return TerminalReport(not failed, failed)


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_KEYS: set = set()


def _acquire_cluster(keys: list) -> bool:
    """M-4-disjoint: claim a cluster's item keys; refuse if any key is already in another in-flight
    commit (a COMPUTATIONAL serialization guard — not LLM-agent prose, per the safety-is-computational
    doctrine). The engine is single-process, so a lock + set suffices."""
    with _INFLIGHT_LOCK:
        if any(k in _INFLIGHT_KEYS for k in keys):
            return False
        _INFLIGHT_KEYS.update(keys)
        return True


def _release_cluster(keys: list) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT_KEYS.difference_update(keys)


def commit_merge(
    snapshot: ClusterSnapshot,
    reader: Any,
    gateway: Any,
    prov: ProvenanceStore,
    *,
    library_id: int,
    library_type: str = "user",
    smart_fill: bool = False,
    now: Optional[datetime] = None,
    ceiling: int = DEFAULT_CEILING,
    freshness_window: float = DEFAULT_FRESHNESS_WINDOW,
    field_sources: Optional[dict] = None,
) -> CommitResult:
    """M-4-disjoint serialization wrapper: claim the cluster's item keys so two overlapping clusters
    cannot be committed concurrently (a destructive op on a shared item must serialize), then delegate to
    the fail-closed inner commit. Releases the claim in ``finally``."""
    cluster_keys = [snapshot.master_key, *snapshot.secondary_keys]
    if not _acquire_cluster(cluster_keys):
        return CommitResult(mode="blocked",
                            reason="cluster shares an item with another in-flight commit (M-4-disjoint)")
    try:
        return _commit_merge_inner(
            snapshot, reader, gateway, prov, library_id=library_id, library_type=library_type,
            smart_fill=smart_fill, now=now, ceiling=ceiling, freshness_window=freshness_window,
            field_sources=field_sources)
    finally:
        _release_cluster(cluster_keys)


def _commit_merge_inner(
    snapshot: ClusterSnapshot,
    reader: Any,
    gateway: Any,
    prov: ProvenanceStore,
    *,
    library_id: int,
    library_type: str = "user",
    smart_fill: bool = False,
    now: Optional[datetime] = None,
    ceiling: int = DEFAULT_CEILING,
    freshness_window: float = DEFAULT_FRESHNESS_WINDOW,
    field_sources: Optional[dict] = None,
) -> CommitResult:
    """Verify-gated, fail-closed commit. Re-verify the post-PATCH state, then TRASH the secondaries
    (PATCH ``deleted:1`` — never delete/purge), re-assert children (M-4), terminal-verify the final state
    (C-5), and log two-phase PROV (C-3). It TRASHES only when the out-of-band enable token is present AND
    observability is fresh; otherwise it runs in SHADOW (verify + log, no trash). Any failure after the
    PATCH phase routes to ``rollback_merge``.

    Reader contract: ``get_children`` MUST include trashed children (so M-4 / terminal verify can see a
    cascade-trashed child).
    """
    now = now or datetime.now(timezone.utc)
    m = snapshot.master_key
    sec = list(snapshot.secondary_keys)

    # GATE (H-1): per-commit secondary ceiling — refuse pathological clusters (no unbounded trash).
    if len(sec) > ceiling:
        return CommitResult(mode="blocked",
                            reason=f"cluster has {len(sec)} secondaries > ceiling {ceiling}; escalate to human review")

    # Re-read the post-PATCH state (secondaries still alive) and run the 11-check gate.
    observed = build_cluster(reader, m, sec)
    report = verify_merge(snapshot, observed, smart_fill=smart_fill, field_sources=field_sources)
    if not report.passed:
        # OBS-4: record the verify FAIL so it enters the verify-pass-rate denominator.
        prov.record(activity="commit_merge_verify", item_key=m, snapshot_id=snapshot.snapshot_id,
                    agent="merge-engine", tool_version=__version__,
                    params={"pass": False, "failed": [c.name for c in report.failed]})
        # M-3: master was PATCHed by merge_cluster, nothing trashed yet -> rollback state b (not just block).
        rb = rollback_merge(snapshot, observed, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode=("rolled_back" if rb.ok else "rollback_failed"), verify_passed=False,
                            rollback=rb, reason=f"verify failed: {[c.name for c in report.failed]}")

    # GATE (C-1 enable token): default SHADOW — verify passed, log it, NO trash.
    if not live_merge_enabled():
        prov.record(activity="commit_merge_shadow", item_key=m, snapshot_id=snapshot.snapshot_id,
                    agent="merge-engine", tool_version=__version__,
                    params={"pass": True, "would_trash": sec, "reason": "live merge not enabled"})
        return CommitResult(mode="shadow", verify_passed=True,
                            reason="ZOT_MERGE_LIVE_ENABLED not set — verify passed, no trash (shadow)")

    # GATE (C-2 observability freshness): fail-closed if the daily report is stale/absent/non-ok.
    if not observability_is_fresh(prov, window_seconds=freshness_window, now=now):
        return CommitResult(mode="blocked", verify_passed=True,
                            reason="observability stale/absent — fail-closed (no fresh daily_report)")

    # MC-3: record the intended smart_fill key/values so a server-dropped fill is auditable from PROV.
    smart_filled: dict = {}
    if smart_fill:
        proj_m = compute_merge_projection(
            snapshot, smart_fill=True, library_base=library_item_base(library_type, library_id)).items[m]
        sm_fields = snapshot.items[m].fields
        smart_filled = {k: v for k, v in proj_m.fields.items()
                        if _is_empty(sm_fields.get(k)) and not _is_empty(v)}

    # C-3: INTENT PROV before any trash (a trashed-but-no-result secondary stays findable for rollback).
    intent = prov.record(activity="commit_merge_intent", item_key=m, snapshot_id=snapshot.snapshot_id,
                         agent="merge-engine", tool_version=__version__,
                         params={"secondaries": sec,
                                 "expected_versions": {k: observed.items[k].version for k in sec},
                                 "smart_fill": smart_fill, "smart_filled_fields": smart_filled,
                                 "field_sources": field_sources})

    # TRASH each secondary via PATCH deleted:1 (TRASH-NOT-PURGE), sequentially (gateway honors Backoff).
    # retry_on_412=False so a concurrent edit ABORTS to rollback (F6); the handler catches ANY failure
    # (412, 5xx, transport drop) -> rollback the done subset (F1/M-5), not just ConcurrencyConflictError.
    trashed: list = []
    try:
        for k in sec:
            gateway.update_item(library_id, k, {"deleted": 1}, observed.items[k].version,
                                library_type=library_type, retry_on_412=False)
            trashed.append(k)
            # F4: a per-secondary durable record (not just the aggregate intent) so the PROV log reflects
            # exactly which secondaries are actually trashed — the crash-recovery reconcile keys on this.
            prov.record(activity="commit_merge_trashed", item_key=k, snapshot_id=snapshot.snapshot_id,
                        agent="merge-engine", tool_version=__version__, params={"trashed": True})
    except Exception as e:
        post = build_cluster(reader, m, sec)
        rb = rollback_merge(snapshot, post, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode=("rolled_back" if rb.ok else "rollback_failed"),
                            trashed=trashed, rollback=rb, intent_prov_id=intent["prov_id"],
                            reason=f"partial-trash failure -> rollback: {type(e).__name__}: {e}")

    # M-4: post-commit child re-assert (reader includes trashed children).
    post = build_cluster(reader, m, sec)
    if not _reassert_children(snapshot, post, gateway, library_id, library_type):
        rb = rollback_merge(snapshot, post, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode=("rolled_back" if rb.ok else "rollback_failed"), trashed=trashed,
                            rollback=rb, intent_prov_id=intent["prov_id"], reason="M-4 child re-assert failed")

    # C-5: terminal verify of the FINAL state.
    final = build_cluster(reader, m, sec)
    terminal = _terminal_verify(snapshot, final, sec,
                                library_base=library_item_base(library_type, library_id),
                                smart_fill=smart_fill, field_sources=field_sources)
    if not terminal.passed:
        # OBS-4: the terminal verify is a verify -> record its FAIL into the rate.
        prov.record(activity="commit_merge_verify", item_key=m, snapshot_id=snapshot.snapshot_id,
                    agent="merge-engine", tool_version=__version__,
                    params={"pass": False, "failed": terminal.failed})
        rb = rollback_merge(snapshot, final, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode=("rolled_back" if rb.ok else "rollback_failed"), trashed=trashed,
                            rollback=rb, intent_prov_id=intent["prov_id"], reason=f"terminal verify failed: {terminal.failed}")

    # C-3: RESULT PROV (before/after blobs for the sampled audit).
    prov.record(activity="commit_merge", item_key=m, snapshot_id=snapshot.snapshot_id,
                before=snapshot.to_json(), after=final.to_json(),
                agent="merge-engine", tool_version=__version__,
                params={"pass": True, "trashed": trashed})
    return CommitResult(mode="committed", verify_passed=True, trashed=trashed,
                        intent_prov_id=intent["prov_id"])


# ── F4 crash-recovery: reconcile orphaned commit_merge_intent records ───────────

def find_orphan_commit_intents(prov: ProvenanceStore) -> list:
    """``commit_merge_intent`` records with no matching ``commit_merge`` result and not yet reconciled —
    the process died between the intent and the result (mid-trash). These are the orphans crash-recovery
    must roll back."""
    recs = prov.all_records()
    done = {r.get("was_derived_from") for r in recs if r.get("activity") == "commit_merge"}
    reconciled = {r.get("was_derived_from") for r in recs if r.get("activity") == "commit_merge_reconciled"}
    return [r for r in recs if r.get("activity") == "commit_merge_intent"
            and r.get("was_derived_from") not in done
            and r.get("was_derived_from") not in reconciled]


def reconcile_orphan_commits(prov: ProvenanceStore, reader: Any, gateway: Any, *,
                             library_id: int, library_type: str = "user") -> list:
    """F4 crash-recovery: roll back every orphaned commit_merge_intent. Reconstructs the snapshot from its
    ``snapshot_cluster`` PROV before-image blob (ADR-008: snapshot_id IS the rollback index), re-reads the
    live cluster, runs ``rollback_merge``, and records a ``commit_merge_reconciled`` PROV row. Returns the
    per-orphan outcomes. Run at startup before resuming the merge chain."""
    snap_blob = {r.get("was_derived_from"): (r.get("entity") or {}).get("before_blob")
                 for r in prov.all_records() if r.get("activity") == "snapshot_cluster"}
    outcomes: list = []
    for orphan in find_orphan_commit_intents(prov):
        sid = orphan.get("was_derived_from")
        blob = snap_blob.get(sid)
        if not blob:
            outcomes.append({"snapshot_id": sid, "status": "no-snapshot-blob"})
            continue
        snapshot = cluster_snapshot_from_dict(prov.get_json_blob(blob))
        observed = build_cluster(reader, snapshot.master_key, list(snapshot.secondary_keys))
        rb = rollback_merge(snapshot, observed, gateway, library_id=library_id, library_type=library_type)
        prov.record(activity="commit_merge_reconciled", item_key=snapshot.master_key, snapshot_id=sid,
                    agent="merge-engine", tool_version=__version__,
                    params={"rollback_ok": rb.ok, "failures": rb.failures})
        outcomes.append({"snapshot_id": sid, "status": "reconciled" if rb.ok else "rollback_failed",
                         "rollback": rb})
    return outcomes


# ── live reader + snapshot loader (for the MCP tool layer) ──────────────────────

class WebClusterReader:
    """Live :class:`~zotero_write_mcp.merge.ClusterReader` over a ZoteroClient — version-accurate web GETs;
    ``get_children`` includes TRASHED children (``?includeTrashed=1``) so M-4 / the terminal verify can see
    a cascade-trashed child."""

    def __init__(self, client: Any, library_id: int):
        self._c = client
        self._lib = library_id

    def get_item(self, key: str) -> dict:
        return self._c._web_get(f"/users/{self._lib}/items/{key}")

    def get_children(self, key: str) -> list:
        return self._c._web_get(f"/users/{self._lib}/items/{key}/children", {"includeTrashed": 1})

    def get_annotations(self, attachment_key: str) -> list:
        # Zotero's /children endpoint returns 400 ("can only be called on PDF, EPUB, and snapshot
        # attachments") for any other attachment type — those simply carry no annotations. Treat that as an
        # empty annotation list rather than letting it abort the whole snapshot_cluster / verify chain.
        try:
            children = self.get_children(attachment_key)
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 400:
                return []
            raise
        return [c for c in children if c.get("data", {}).get("itemType") == "annotation"]

    def get_citekey(self, key: str) -> Optional[str]:
        return None   # BBT JSON-RPC citekey lookup — TODO; None is safe for check #11 (preserve-unchanged)


def load_snapshot(prov: ProvenanceStore, snapshot_id: str) -> Optional[ClusterSnapshot]:
    """Reconstruct a :class:`ClusterSnapshot` from its ``snapshot_cluster`` PROV before-image blob — the
    bridge the merge_cluster/commit_merge/rollback_merge MCP tools use to load a snapshot by id."""
    for r in prov.all_records():
        if r.get("activity") == "snapshot_cluster" and r.get("was_derived_from") == snapshot_id:
            blob = (r.get("entity") or {}).get("before_blob")
            if blob:
                return cluster_snapshot_from_dict(prov.get_json_blob(blob))
    return None
