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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from zotero_write_mcp import __version__
from zotero_write_mcp.gateway import ConcurrencyConflictError, library_prefix
from zotero_write_mcp.merge import (
    ClusterSnapshot, RestoreReport, build_cluster, compute_merge_projection, rollback_merge,
    verify_merge, _is_empty, _is_trashed, _unwrap, _zotero_tags,
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


def _terminal_verify(snapshot, final, sec_keys) -> TerminalReport:
    """C-5: scoped re-verify of the FINAL committed state (the full 11-check verify cannot run here — the
    secondaries' versions legitimately changed via the trash). Asserts: every child live + parented to
    master; annotation parity + storage integrity per attachment; every secondary present AND trashed."""
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
    return TerminalReport(not failed, failed)


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
    report = verify_merge(snapshot, observed, smart_fill=smart_fill)
    if not report.passed:
        # M-3: master was PATCHed by merge_cluster, nothing trashed yet -> rollback state b (not just block).
        rb = rollback_merge(snapshot, observed, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode="rolled_back", verify_passed=False, rollback=rb,
                            reason=f"verify failed: {[c.name for c in report.failed]}")

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

    # C-3: INTENT PROV before any trash (a trashed-but-no-result secondary stays findable for rollback).
    intent = prov.record(activity="commit_merge_intent", item_key=m, snapshot_id=snapshot.snapshot_id,
                         agent="merge-engine", tool_version=__version__,
                         params={"secondaries": sec,
                                 "expected_versions": {k: observed.items[k].version for k in sec}})

    # TRASH each secondary via PATCH deleted:1 (TRASH-NOT-PURGE), sequentially (gateway honors Backoff, M-5).
    trashed: list = []
    try:
        for k in sec:
            gateway.update_item(library_id, k, {"deleted": 1}, observed.items[k].version,
                                library_type=library_type)
            trashed.append(k)
    except ConcurrencyConflictError as e:
        # M-5: partial trash (412) -> rollback (untrash the done subset + revert master).
        post = build_cluster(reader, m, sec)
        rb = rollback_merge(snapshot, post, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode="rolled_back", trashed=trashed, rollback=rb, intent_prov_id=intent["prov_id"],
                            reason=f"partial trash (412): {e}")

    # M-4: post-commit child re-assert (reader includes trashed children).
    post = build_cluster(reader, m, sec)
    if not _reassert_children(snapshot, post, gateway, library_id, library_type):
        rb = rollback_merge(snapshot, post, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode="rolled_back", trashed=trashed, rollback=rb, intent_prov_id=intent["prov_id"],
                            reason="M-4 child re-assert failed")

    # C-5: terminal verify of the FINAL state.
    final = build_cluster(reader, m, sec)
    terminal = _terminal_verify(snapshot, final, sec)
    if not terminal.passed:
        rb = rollback_merge(snapshot, final, gateway, library_id=library_id, library_type=library_type)
        return CommitResult(mode="rolled_back", trashed=trashed, rollback=rb, intent_prov_id=intent["prov_id"],
                            reason=f"terminal verify failed: {terminal.failed}")

    # C-3: RESULT PROV (before/after blobs for the sampled audit).
    prov.record(activity="commit_merge", item_key=m, snapshot_id=snapshot.snapshot_id,
                before=snapshot.to_json(), after=final.to_json(),
                agent="merge-engine", tool_version=__version__,
                params={"pass": True, "trashed": trashed})
    return CommitResult(mode="committed", verify_passed=True, trashed=trashed,
                        intent_prov_id=intent["prov_id"])
