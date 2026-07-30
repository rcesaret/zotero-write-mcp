"""Routine Supervised v1.0 — the production merge transaction (PRD 02_v1_prd.md §6).

ONE engine-owned state machine replaces the agent-sequenced snapshot→merge→commit chain for
production use: the agent (after owner approval) makes ONE call carrying an immutable proposal
identifier; the engine performs fresh read, version capture, conservative projection, apply,
pre-trash verify, trash, and terminal verify internally (MRG-005). There is no ``smart_fill``,
no ``field_sources``, and no version parameter on this surface — enrichment is a separately
reviewed metadata operation (META-*), and version continuity is engine-owned and mandatory
(MRG-001/002, CON-001).

Concurrency doctrine (CON-002/003/004): conflict means STOP, not overwrite. Every engine write is
recorded as a durable receipt ``(op, key, prior_version, new_version)``. Recovery inverts ONLY
receipts whose item still sits at the engine-produced version — proof that no external writer
intervened. Any unexplained version means a possible concurrent edit: inversion stops, the exact
live state is preserved byte-for-byte, and the transaction terminates ``unresolved``, which blocks
every later destructive transaction until resolved (REC-003/006).

Terminal states are explicit and exclusive (REC-002): ``shadow`` | ``blocked_before_write`` |
``committed`` | ``rolled_back`` | ``unresolved`` (plus ``already_committed`` for an idempotent
retry, IDEM-001). Agent narration cannot upgrade any of them (IDEM-003 is enforced harness-side by
copying this structured result verbatim).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from zotero_write_mcp import __version__
from zotero_write_mcp.gateway import ConcurrencyConflictError
from zotero_write_mcp.merge import (
    ClusterSnapshot, build_cluster, compute_merge_projection, verify_merge,
    _is_empty, _is_trashed, _master_overrides, _unwrap, _zotero_tags,
)
from zotero_write_mcp.merge_live import (
    DEFAULT_CEILING, _acquire_cluster, _release_cluster, _terminal_verify,
    library_item_base, live_merge_enabled, unresolved_transactions,
)
from zotero_write_mcp.provenance import ProvenanceStore, canonical_json, sha256_hex

# PROV activities of the transaction layer. `unresolved_transactions` (merge_live) treats
# TXN_UNRESOLVED as blocking until a TXN_RESOLVED names the same transaction_id, and treats an
# orphaned TXN_INTENT (crash before a terminal record) the same way via `find_orphan_txn_intents`.
TXN_PROPOSED = "merge_txn_proposed"
TXN_INTENT = "merge_txn_intent"
TXN_WRITE_INTENT = "merge_txn_write_intent"   # appended BEFORE each gateway write (write-ahead)
TXN_WRITE = "merge_txn_write"                 # appended AFTER the write succeeds (carries new_version)
TXN_SHADOW = "merge_txn_shadow"
TXN_RESULT = "merge_txn_result"          # committed
TXN_BLOCKED = "merge_txn_blocked"        # blocked_before_write — no library mutation occurred
TXN_ROLLED_BACK = "merge_txn_rolled_back"
TXN_UNRESOLVED = "merge_txn_unresolved"
TXN_RESOLVED = "merge_txn_resolved"

_TERMINAL_ACTIVITIES = frozenset({TXN_RESULT, TXN_ROLLED_BACK, TXN_UNRESOLVED,
                                  TXN_BLOCKED, TXN_SHADOW})


@dataclass
class TxnResult:
    """The structured outcome the agent must relay verbatim (IDEM-002/003)."""
    transaction_id: str
    state: str                       # shadow | blocked_before_write | committed | rolled_back |
                                     # unresolved | already_committed
    reason: str = ""
    live_write_performed: bool = False
    writes_applied: list = field(default_factory=list)
    trashed: list = field(default_factory=list)
    verify: Optional[dict] = None
    terminal: Optional[dict] = None
    recovery: Optional[dict] = None
    snapshot_id: Optional[str] = None
    next_action: str = ""

    def to_dict(self) -> dict:
        return {
            "transaction_id": self.transaction_id, "state": self.state, "reason": self.reason,
            "live_write_performed": self.live_write_performed,
            "writes_applied": self.writes_applied, "trashed": self.trashed,
            "verify": self.verify, "terminal": self.terminal, "recovery": self.recovery,
            "snapshot_id": self.snapshot_id, "next_action": self.next_action,
        }


def _txn_id(master_key: str, secondary_keys: list, member_versions: dict) -> str:
    """Deterministic, content-bound transaction id: the same cluster at the same versions always
    proposes the same id, and ANY change to the member set or any member's version produces a
    different id — so an approval can never silently carry over to changed state (SCOPE-003)."""
    basis = {"master": master_key, "secondaries": sorted(secondary_keys),
             "versions": {k: int(v) for k, v in sorted(member_versions.items())}}
    return "TXN-" + sha256_hex(canonical_json(basis))[:16]


def propose_merge_txn(reader: Any, prov: ProvenanceStore, master_key: str, secondary_keys: list, *,
                      library_id: int, library_type: str = "user") -> dict:
    """Read-only proposal: fresh cluster read + conservative projection preview, recorded immutably.

    The owner approves EXACTLY this: the named survivor, the named secondaries, at the captured
    versions. The returned/recorded ``transaction_id`` is derived from that content (PRV-001,
    SCOPE-003). The preview is labeled non-authoritative (PRV-002): its verify runs against the
    projection, which is self-consistent by construction.
    """
    fresh = build_cluster(reader, master_key, list(secondary_keys))
    member_versions = {k: it.version for k, it in fresh.items.items()}
    tid = _txn_id(master_key, list(secondary_keys), member_versions)
    base = library_item_base(library_type, library_id)
    projection = compute_merge_projection(fresh, smart_fill=False, library_base=base)
    report = verify_merge(fresh, projection)
    members = {}
    for k, it in fresh.items.items():
        members[k] = {
            "role": "survivor" if k == master_key else "secondary",
            "version": it.version, "item_type": it.item_type, "citekey": it.citekey,
            "trashed": _is_trashed(it),
            "n_children": len(fresh.children_of(k)),
            "collections": it.collections, "tags": it.tags,
        }
    proposal = {
        "transaction_id": tid,
        "master_key": master_key,
        "secondary_keys": list(secondary_keys),
        "member_versions": member_versions,
        "members": members,
        "operations": (["patch-master (collections/tags/relations union + dc:replaces"
                        " + citekey-alias accumulation)"]
                       + [f"reparent {c.key} -> {master_key}"
                          for c in (fresh.notes + fresh.attachments) if c.parent_key != master_key]
                       + [f"trash {k} (recoverable; never purged)" for k in secondary_keys]),
        "survivor_after": {
            "collections": projection.items[master_key].collections,
            "tags": projection.items[master_key].tags,
            "extra": projection.items[master_key].fields.get("extra"),
            "citekey": projection.items[master_key].citekey,
        },
        "projection_verify_pass": report.passed,
        "verified_against": "projection",
        "gate_authoritative": False,
        "warning": ("PREVIEW ONLY — this verify ran against the self-consistent projection, not live "
                    "post-merge state. The live gate re-verifies during execution. Approval of this "
                    "transaction_id is void if any member changes (a changed version changes the id)."),
        "enrichment": "none: smart_fill and field_sources do not exist on this surface (MRG-001/002)",
    }
    prov.record(activity=TXN_PROPOSED, item_key=master_key, agent="merge-txn",
                tool_version=__version__,
                params={"transaction_id": tid, "master_key": master_key,
                        "secondary_keys": list(secondary_keys),
                        "member_versions": member_versions})
    return proposal


def find_orphan_txn_intents(prov: ProvenanceStore) -> list:
    """TXN_INTENT records whose transaction reached NO terminal record — the process died mid-
    transaction. These block destructive work (REC-006) until reconciled."""
    terminal: set = set()
    intents: dict = {}
    for r in prov.iter_records():
        act = r.get("activity")
        p = r.get("params") or {}
        if act in _TERMINAL_ACTIVITIES:
            terminal.add(p.get("transaction_id"))
        elif act == TXN_INTENT:
            tid = p.get("transaction_id")
            if tid:
                intents[tid] = r
    return [r for tid, r in intents.items() if tid not in terminal]


def _txn_receipts(prov: ProvenanceStore, transaction_id: str) -> list:
    return [r for r in prov.iter_records()
            if r.get("activity") == TXN_WRITE
            and (r.get("params") or {}).get("transaction_id") == transaction_id]


def _pending_write_intents(prov: ProvenanceStore, transaction_id: str) -> list:
    """TXN_WRITE_INTENT records with no matching TXN_WRITE receipt — the process may have died
    between the library write and the receipt append, so whether the write landed is unknown until
    classified against live content."""
    receipts = {((r.get("params") or {}).get("op"), (r.get("params") or {}).get("key"))
                for r in _txn_receipts(prov, transaction_id)}
    return [r.get("params") or {} for r in prov.iter_records()
            if r.get("activity") == TXN_WRITE_INTENT
            and (r.get("params") or {}).get("transaction_id") == transaction_id
            and ((r.get("params") or {}).get("op"), (r.get("params") or {}).get("key")) not in receipts]


def _classify_pending(pending: dict, snapshot: ClusterSnapshot, reader: Any) -> tuple:
    """Did an unreceipted write land? Returns ``(verdict, current_version)`` with verdict in
    {"landed", "not-landed", "ambiguous"}, judged by CONTENT against the snapshot + intended body —
    version arithmetic alone cannot distinguish the engine's write from an external edit."""
    op, key, body = pending["op"], pending["key"], pending.get("body") or {}
    raw = reader.get_item(key)
    _, cur_ver, cur = _unwrap(raw)
    if cur_ver == pending["prior_version"]:
        return "not-landed", cur_ver
    if op == "trash":
        # Trashed -> the trash landed (or an equivalent external trash; untrash is safe either way).
        # Not trashed at a different version -> the trash did not land; scalar edits are irrelevant.
        return ("landed", cur_ver) if cur.get("deleted") in (1, "1", True) else ("not-landed", cur_ver)
    if op == "reparent":
        target = body.get("parentItem")
        if cur.get("parentItem") == target:
            return "landed", cur_ver
        snap_parent = next((c.parent_key for c in (snapshot.notes + snapshot.attachments)
                            if c.key == key), None)
        return ("not-landed", cur_ver) if cur.get("parentItem") == snap_parent else ("ambiguous", cur_ver)
    if op == "patch-master":
        applied = dict(snapshot.items[snapshot.master_key].json)
        applied.update(body)
        keys = list(body.keys())
        def _norm(d):
            out = {}
            for k in keys:
                v = d.get(k)
                if k == "collections":
                    out[k] = set(v or [])
                elif k == "tags":
                    out[k] = {(t.get("tag"), int(t.get("type", 0))) if isinstance(t, dict) else tuple(t)
                              for t in (v or [])}
                elif k == "relations":
                    out[k] = {p: set(vv if isinstance(vv, list) else [vv])
                              for p, vv in (v or {}).items()}
                else:
                    out[k] = v
            return out
        if _norm(cur) == _norm(applied):
            return "landed", cur_ver
        if _norm(cur) == _norm(snapshot.items[snapshot.master_key].json):
            return "not-landed", cur_ver
        return "ambiguous", cur_ver
    return "ambiguous", cur_ver


def _new_version_of(write_result: Any, reader: Any, key: str) -> int:
    v = getattr(write_result, "last_modified_version", None)
    if v is not None:
        return int(v)
    return _unwrap(reader.get_item(key))[1]


class _Txn:
    """Execution context for one transaction: records receipts and performs receipt-based inversion."""

    def __init__(self, prov: ProvenanceStore, reader: Any, gateway: Any, snapshot: ClusterSnapshot,
                 transaction_id: str, *, library_id: int, library_type: str):
        self.prov, self.reader, self.gateway = prov, reader, gateway
        self.snap, self.tid = snapshot, transaction_id
        self.lib, self.lib_type = library_id, library_type
        self.receipts: list = []

    def write(self, op: str, key: str, body: dict, version: int, *, invertible: bool = True) -> None:
        """One engine write, journaled write-ahead. The TXN_WRITE_INTENT lands durably BEFORE the
        gateway call and the TXN_WRITE receipt (with the engine-produced new_version) lands after —
        so a crash between the library write and the receipt leaves a pending intent that recovery
        classifies by content instead of silently forgetting the landed write (REC-001).
        retry_on_412 is ALWAYS False on this surface — a 412 is a concurrent edit and must stop the
        transaction, never blind-retry (CON-002)."""
        pending = {"transaction_id": self.tid, "op": op, "key": key,
                   "prior_version": int(version), "body": dict(body), "invertible": invertible}
        self.prov.record(activity=TXN_WRITE_INTENT, item_key=key, snapshot_id=self.snap.snapshot_id,
                         agent="merge-txn", tool_version=__version__, params=pending)
        res = self.gateway.update_item(self.lib, key, body, version,
                                       library_type=self.lib_type, retry_on_412=False)
        new_ver = _new_version_of(res, self.reader, key)
        receipt = {"transaction_id": self.tid, "op": op, "key": key,
                   "prior_version": int(version), "new_version": int(new_ver),
                   "body_keys": sorted(body.keys()), "invertible": invertible}
        self.prov.record(activity=TXN_WRITE, item_key=key, snapshot_id=self.snap.snapshot_id,
                         agent="merge-txn", tool_version=__version__, params=receipt)
        self.receipts.append(receipt)

    # ── receipt-based inversion (CON-003: only over engine-proven state) ────────

    def _inverse_body(self, receipt: dict) -> Optional[dict]:
        snap = self.snap
        op, key = receipt["op"], receipt["key"]
        if op == "patch-master":
            sm = snap.items[snap.master_key]
            body = {"collections": sm.collections, "tags": _zotero_tags(sm.tags),
                    "relations": sm.relations}
            if "extra" in receipt["body_keys"]:
                body["extra"] = sm.fields.get("extra") or ""
            return body
        if op == "reparent":
            for c in (snap.notes + snap.attachments):
                if c.key == key:
                    return {"parentItem": c.parent_key}
            return None
        if op == "trash":
            return {"deleted": 0}
        return None                                   # reassert etc.: non-invertible no-ops

    def invert(self) -> dict:
        """Invert every invertible receipt, newest first. Each inversion requires the item to still
        sit at the receipt's engine-produced ``new_version`` — the computational proof that no
        external writer touched it since. Any mismatch or inversion failure stops with
        ``complete=False`` and the exact blocking evidence; nothing is overwritten on faith."""
        inverted: list = []
        for receipt in reversed(self.receipts):
            if not receipt.get("invertible", True):
                continue
            body = self._inverse_body(receipt)
            if body is None:
                continue
            key = receipt["key"]
            cur_ver = _unwrap(self.reader.get_item(key))[1]
            if cur_ver != receipt["new_version"]:
                return {"complete": False, "inverted": inverted,
                        "blocked_at": {"op": receipt["op"], "key": key,
                                       "expected_version": receipt["new_version"],
                                       "observed_version": cur_ver},
                        "reason": ("version advanced beyond the engine's own write — a concurrent "
                                   "edit may be present; inversion stopped without overwriting it")}
            try:
                self.gateway.update_item(self.lib, key, body, cur_ver,
                                         library_type=self.lib_type, retry_on_412=False)
            except Exception as e:
                return {"complete": False, "inverted": inverted,
                        "blocked_at": {"op": receipt["op"], "key": key,
                                       "error": f"{type(e).__name__}: {e}"},
                        "reason": "an inversion write failed"}
            inverted.append({"op": receipt["op"], "key": key})
        return {"complete": True, "inverted": inverted}


def execute_merge_txn(transaction_id: str, reader: Any, gateway: Any, prov: ProvenanceStore, *,
                      library_id: int, library_type: str = "user",
                      ceiling: int = DEFAULT_CEILING) -> TxnResult:
    """Execute (or shadow) ONE approved merge transaction. See module docstring for the contract."""
    tid = transaction_id

    def blocked(reason: str, next_action: str) -> TxnResult:
        prov.record(activity=TXN_BLOCKED, agent="merge-txn", tool_version=__version__,
                    params={"transaction_id": tid, "reason": reason})
        return TxnResult(tid, "blocked_before_write", reason=reason, next_action=next_action)

    # ── Gate 0: evidence integrity, unresolved state, proposal, idempotency ────
    integrity = prov.scan_integrity()
    if integrity["status"] == "blocked":
        return blocked(
            f"PROV log integrity is blocked: {len(integrity['unaccepted'])} unaccepted damaged "
            "line(s) (LOG-001).",
            "Inspect and resolve with scripts/accept_prov_damage.py, then re-run.")

    # Idempotency BEFORE the unresolved gate for the committed case (IDEM-001): a completed
    # transaction's outcome is returned as recorded even if some OTHER transaction is unresolved.
    prior_result = None
    proposal = None
    for r in prov.iter_records():
        p = r.get("params") or {}
        if p.get("transaction_id") != tid:
            continue
        if r.get("activity") == TXN_RESULT:
            prior_result = r
        elif r.get("activity") == TXN_PROPOSED:
            proposal = r
    if prior_result is not None:
        pp = prior_result.get("params") or {}
        return TxnResult(tid, "already_committed",
                         reason="this transaction already committed; no write was repeated (IDEM-001)",
                         trashed=pp.get("trashed") or [],
                         snapshot_id=prior_result.get("was_derived_from"),
                         next_action="None — inspect query_provenance for the recorded outcome.")

    unresolved = unresolved_transactions(prov)     # includes orphaned txn intents (merge_live)
    if unresolved:
        names = ", ".join(str(u.get("transaction_id") or u.get("snapshot_id")) for u in unresolved)
        return blocked(
            f"{len(unresolved)} unresolved transaction(s) exist ({names}); destructive work is "
            "refused until resolved (REC-006).",
            "Run reconcile_orphans / the recovery runbook; failed recoveries need human resolution.")

    if proposal is None:
        return blocked(f"unknown transaction_id {tid!r} — no recorded proposal.",
                       "Call propose_merge_txn and obtain owner approval first.")
    pp = proposal.get("params") or {}
    m = pp["master_key"]
    sec = list(pp["secondary_keys"])
    member_versions = {k: int(v) for k, v in (pp.get("member_versions") or {}).items()}

    if len(sec) > ceiling:
        return blocked(f"cluster has {len(sec)} secondaries > ceiling {ceiling} (H-1).",
                       "Split the cluster or escalate to human review.")

    cluster_keys = [m, *sec]
    if not _acquire_cluster(cluster_keys):
        return blocked("cluster shares an item with another in-flight commit (M-4-disjoint).",
                       "Wait for the other transaction to finish, then re-propose.")
    try:
        # ── Fresh read; the proposal must still exactly describe reality ───────
        snap = build_cluster(reader, m, sec)
        stale = {k: {"proposed": member_versions.get(k), "live": snap.items[k].version}
                 for k in cluster_keys
                 if snap.items.get(k) is None or snap.items[k].version != member_versions.get(k)}
        if stale:
            return blocked(f"proposal is stale — member versions changed since approval: {stale} "
                           "(SCOPE-003/CON-002).",
                           "Re-run propose_merge_txn and obtain fresh owner approval.")
        trashed_members = [k for k in cluster_keys if _is_trashed(snap.items[k])]
        if trashed_members:
            return blocked(f"member(s) already in trash: {trashed_members}; this surface merges live "
                           "items only.",
                           "Restore or exclude the trashed member(s), then re-propose.")

        base = library_item_base(library_type, library_id)

        # ── Shadow mode: verify pipeline + log, no library mutation (MUT-002) ──
        if not live_merge_enabled():
            projection = compute_merge_projection(snap, smart_fill=False, library_base=base)
            report = verify_merge(snap, projection)
            prov.record(activity=TXN_SHADOW, item_key=m, snapshot_id=snap.snapshot_id,
                        agent="merge-txn", tool_version=__version__,
                        params={"transaction_id": tid, "pass": report.passed,
                                "verified_against": "projection", "would_trash": sec})
            return TxnResult(tid, "shadow",
                             reason="live merge not enabled (out-of-band token absent) — projection "
                                    "verified, NO library mutation performed",
                             verify={"pass": report.passed, "verified_against": "projection",
                                     "gate_authoritative": False},
                             snapshot_id=snap.snapshot_id,
                             next_action="Owner: set the enable token for the approved window, then "
                                         "re-run this exact transaction.")

        # ── Durable before-image + intent BEFORE the first mutation (REC-001) ──
        prov.record(activity="snapshot_cluster", item_key=m, before=snap.to_json(),
                    snapshot_id=snap.snapshot_id, agent="merge-txn", tool_version=__version__,
                    params={"transaction_id": tid, "master_key": m, "secondary_keys": sec,
                            "n_notes": len(snap.notes), "n_attachments": len(snap.attachments)})
        prov.record(activity=TXN_INTENT, item_key=m, snapshot_id=snap.snapshot_id,
                    agent="merge-txn", tool_version=__version__,
                    params={"transaction_id": tid, "master_key": m, "secondary_keys": sec,
                            "member_versions": member_versions})

        txn = _Txn(prov, reader, gateway, snap, tid, library_id=library_id, library_type=library_type)

        def unresolved_result(reason: str, recovery: dict) -> TxnResult:
            prov.record(activity=TXN_UNRESOLVED, item_key=m, snapshot_id=snap.snapshot_id,
                        agent="merge-txn", tool_version=__version__,
                        params={"transaction_id": tid, "reason": reason, "recovery": recovery,
                                "writes_applied": txn.receipts})
            return TxnResult(tid, "unresolved", reason=reason, live_write_performed=True,
                             writes_applied=list(txn.receipts),
                             trashed=[r["key"] for r in txn.receipts if r["op"] == "trash"],
                             recovery=recovery, snapshot_id=snap.snapshot_id,
                             next_action="STOP. All destructive transactions are blocked. Follow the "
                                         "recovery runbook; record merge_txn_resolved only after "
                                         "human-verified repair.")

        def rolled_back_result(reason: str, recovery: dict) -> TxnResult:
            prov.record(activity=TXN_ROLLED_BACK, item_key=m, snapshot_id=snap.snapshot_id,
                        agent="merge-txn", tool_version=__version__,
                        params={"transaction_id": tid, "reason": reason, "recovery": recovery})
            return TxnResult(tid, "rolled_back", reason=reason, live_write_performed=True,
                             writes_applied=list(txn.receipts), recovery=recovery,
                             snapshot_id=snap.snapshot_id,
                             next_action="Library restored to pre-transaction state. Re-propose "
                                         "after addressing the cause.")

        def stop(reason: str) -> TxnResult:
            rec = txn.invert()
            if rec["complete"]:
                return rolled_back_result(reason, rec)
            return unresolved_result(reason + " — AND inversion could not complete: " + rec["reason"],
                                     rec)

        # ── Apply: conservative projection PATCH on the master ─────────────────
        proj = compute_merge_projection(snap, smart_fill=False, library_base=base)
        pm = proj.items[m]
        master_body: dict = {"collections": pm.collections, "tags": _zotero_tags(pm.tags),
                             "relations": pm.relations}
        overrides = _master_overrides(snap, None)     # citekey-alias accumulation ONLY (no enrichment)
        for fld, val in overrides.items():
            master_body[fld] = val
        try:
            txn.write("patch-master", m, master_body, snap.items[m].version)
        except ConcurrencyConflictError:
            # First write got 412 atomically: nothing landed anywhere.
            return blocked("concurrent edit detected at the master PATCH (412); no write occurred.",
                           "Re-run propose_merge_txn against the current state.")

        # ── Reparent children ──────────────────────────────────────────────────
        try:
            for c in (snap.notes + snap.attachments):
                if c.parent_key != m:
                    txn.write("reparent", c.key, {"parentItem": m}, c.version)
        except Exception as e:
            return stop(f"child reparent failed ({type(e).__name__}: {e}); no secondary was trashed.")

        # ── Pre-trash verify against a LIVE re-read (gate-authoritative) ───────
        observed = build_cluster(reader, m, sec)
        master_receipt = next(r for r in txn.receipts if r["op"] == "patch-master")
        if observed.items[m].version != master_receipt["new_version"]:
            return stop("concurrent master edit landed between PATCH and verify "
                        f"(version {observed.items[m].version} != engine-produced "
                        f"{master_receipt['new_version']}); no secondary was trashed (CON-002).")
        report = verify_merge(snap, observed)
        verify_payload = {"pass": report.passed, "verified_against": "observed",
                          "gate_authoritative": True,
                          "failed": [c.name for c in report.failed]}
        prov.record(activity="commit_merge_verify", item_key=m, snapshot_id=snap.snapshot_id,
                    agent="merge-txn", tool_version=__version__,
                    params={"transaction_id": tid, "pass": report.passed,
                            "failed": [c.name for c in report.failed]})
        if not report.passed:
            res = stop(f"live verify failed: {[c.name for c in report.failed]}; "
                       "no secondary was trashed.")
            res.verify = verify_payload
            return res

        # ── Trash (PATCH deleted:1 — never purge), pinned versions ─────────────
        try:
            for k in sec:
                txn.write("trash", k, {"deleted": 1}, observed.items[k].version)
        except Exception as e:
            res = stop(f"trash phase failed ({type(e).__name__}: {e}).")
            res.verify = verify_payload
            return res

        # ── Post-trash child re-assert + terminal verify (MRG-007/008) ─────────
        post = build_cluster(reader, m, sec)
        post_children = {c.key: c for c in (post.notes + post.attachments)}
        try:
            for c in (snap.notes + snap.attachments):
                pc = post_children.get(c.key)
                if pc is None:
                    raise RuntimeError(f"child {c.key} vanished after trash")
                if _is_trashed(pc) or pc.parent_key != m:
                    txn.write("reassert-child", c.key, {"deleted": 0, "parentItem": m}, pc.version,
                              invertible=False)
        except Exception as e:
            res = stop(f"child re-assert failed ({type(e).__name__}: {e}).")
            res.verify = verify_payload
            return res

        final = build_cluster(reader, m, sec)
        terminal = _terminal_verify(snap, final, sec, library_base=base)
        terminal_payload = {"pass": terminal.passed, "failed": terminal.failed}
        if not terminal.passed:
            prov.record(activity="commit_merge_verify", item_key=m, snapshot_id=snap.snapshot_id,
                        agent="merge-txn", tool_version=__version__,
                        params={"transaction_id": tid, "pass": False, "failed": terminal.failed})
            res = stop(f"terminal verify failed: {terminal.failed}.")
            res.verify, res.terminal = verify_payload, terminal_payload
            return res

        prov.record(activity=TXN_RESULT, item_key=m, snapshot_id=snap.snapshot_id,
                    before=snap.to_json(), after=final.to_json(),
                    agent="merge-txn", tool_version=__version__,
                    params={"transaction_id": tid, "pass": True, "trashed": sec})
        return TxnResult(tid, "committed", reason="merge committed and terminally verified",
                         live_write_performed=True, writes_applied=list(txn.receipts),
                         trashed=sec, verify=verify_payload, terminal=terminal_payload,
                         snapshot_id=snap.snapshot_id,
                         next_action="Inspect the survivor and trash in Zotero; secondaries remain "
                                     "recoverable in trash (never purged).")
    finally:
        _release_cluster(cluster_keys)


def reconcile_orphan_txns(prov: ProvenanceStore, reader: Any, gateway: Any, *,
                          library_id: int, library_type: str = "user") -> list:
    """Crash recovery for the transaction layer: for each orphaned TXN_INTENT (terminal record never
    written), invert its receipts under the same version-proof rule. Success records
    TXN_ROLLED_BACK (resolving the orphan); anything less records TXN_UNRESOLVED, which keeps
    blocking (REC-003/005). Per-orphan isolation as in reconcile_orphan_commits (REC-004)."""
    outcomes: list = []
    snapshots = {r.get("was_derived_from"): (r.get("entity") or {}).get("before_blob")
                 for r in prov.iter_records() if r.get("activity") == "snapshot_cluster"}
    for orphan in find_orphan_txn_intents(prov):
        p = orphan.get("params") or {}
        tid = p.get("transaction_id")
        try:
            blob = snapshots.get(orphan.get("was_derived_from"))
            if not blob:
                prov.record(activity=TXN_UNRESOLVED, item_key=(orphan.get("entity") or {}).get("item_key"),
                            snapshot_id=orphan.get("was_derived_from"), agent="merge-txn",
                            tool_version=__version__,
                            params={"transaction_id": tid, "reason": "no snapshot blob — human recovery"})
                outcomes.append({"transaction_id": tid, "status": "unresolved",
                                 "reason": "no-snapshot-blob"})
                continue
            from zotero_write_mcp.merge import cluster_snapshot_from_dict
            snapshot = cluster_snapshot_from_dict(prov.get_json_blob(blob))
            txn = _Txn(prov, reader, gateway, snapshot, tid,
                       library_id=library_id, library_type=library_type)
            txn.receipts = [r.get("params") or {} for r in _txn_receipts(prov, tid)]
            # Write-ahead classification: an intent whose receipt never landed is judged by content.
            # "landed" gains a synthetic receipt (inverted like any other); "not-landed" is ignored;
            # "ambiguous" means human recovery — the orphan stays unresolved.
            ambiguous = None
            for pending in _pending_write_intents(prov, tid):
                verdict, cur_ver = _classify_pending(pending, snapshot, reader)
                if verdict == "landed":
                    txn.receipts.append({"transaction_id": tid, "op": pending["op"],
                                         "key": pending["key"],
                                         "prior_version": pending["prior_version"],
                                         "new_version": cur_ver,
                                         "body_keys": sorted((pending.get("body") or {}).keys()),
                                         "invertible": pending.get("invertible", True)})
                elif verdict == "ambiguous":
                    ambiguous = pending
                    break
            if ambiguous is not None:
                rec = {"complete": False, "inverted": [],
                       "blocked_at": {"op": ambiguous["op"], "key": ambiguous["key"]},
                       "reason": "an unreceipted write's outcome is ambiguous against live content "
                                 "— human recovery required"}
            else:
                rec = txn.invert()
            if rec["complete"]:
                prov.record(activity=TXN_ROLLED_BACK, item_key=snapshot.master_key,
                            snapshot_id=snapshot.snapshot_id, agent="merge-txn",
                            tool_version=__version__,
                            params={"transaction_id": tid, "reason": "crash recovery", "recovery": rec})
                outcomes.append({"transaction_id": tid, "status": "rolled_back", "recovery": rec})
            else:
                prov.record(activity=TXN_UNRESOLVED, item_key=snapshot.master_key,
                            snapshot_id=snapshot.snapshot_id, agent="merge-txn",
                            tool_version=__version__,
                            params={"transaction_id": tid, "reason": rec["reason"], "recovery": rec})
                outcomes.append({"transaction_id": tid, "status": "unresolved", "recovery": rec})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            try:
                prov.record(activity=TXN_UNRESOLVED, item_key=None, agent="merge-txn",
                            tool_version=__version__,
                            params={"transaction_id": tid, "reason": err})
            except Exception:
                pass
            outcomes.append({"transaction_id": tid, "status": "error", "error": err})
    return outcomes
