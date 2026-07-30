"""Routine Supervised v1.0 — recovery truthfulness (PRD REC-003/REC-004/REC-005, CON-003 in recovery).

A failed recovery must remain failed and discoverable; one bad orphan must not hide the rest; a
success marker is written only on success; and recovery never restores an old snapshot over a
master state it cannot prove the engine authored (a possible concurrent edit is preserved exactly).
"""
from datetime import datetime, timezone

from zotero_write_mcp.merge import snapshot_cluster
from zotero_write_mcp.merge_live import (
    find_orphan_commit_intents, master_inversion_provable, merge_cluster,
    reconcile_orphan_commits, unresolved_transactions,
)
from zotero_write_mcp.provenance import ProvenanceStore

LIB = 11056739


def _i(key, version, itype, parent=None, **extra):
    data = {"key": key, "version": version, "itemType": itype, **extra}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": version, "data": data}


class FakeLibrary:
    """Mutable library; reader + gateway in one (same shape as test_commit_merge.FakeLibrary)."""

    def __init__(self, items):
        self.items = items
        self.lib_ver = max(it["version"] for it in items.values())

    def get_item(self, key):
        return self.items[key]

    def get_children(self, key):
        return [it for it in self.items.values()
                if it["data"].get("parentItem") == key
                and it["data"].get("itemType") in ("note", "attachment")]

    def get_annotations(self, attachment_key):
        return [it for it in self.items.values()
                if it["data"].get("parentItem") == attachment_key
                and it["data"].get("itemType") == "annotation"]

    def get_citekey(self, key):
        return None

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        it = self.items[item_key]
        self.lib_ver += 1
        it["data"].update(data)
        it["version"] = self.lib_ver
        it["data"]["version"] = self.lib_ver

    def create_items(self, library_id, objects, *, library_type="user"):
        raise AssertionError("create_items must NOT be called on the trash-not-purge path")


class FailingUntrashGateway:
    """Delegates reads to the library; update_item raises for the named keys (forced rollback failure)."""

    def __init__(self, lib, fail_keys):
        self._lib = lib
        self.fail_keys = set(fail_keys)

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        if item_key in self.fail_keys:
            raise RuntimeError(f"forced gateway failure for {item_key}")
        return self._lib.update_item(library_id, item_key, data, version,
                                     library_type=library_type, retry_on_412=retry_on_412)

    def create_items(self, *a, **k):
        raise AssertionError("create_items must NOT be called on the trash-not-purge path")


def make_raw():
    return {
        "M1": _i("M1", 100, "journalArticle", collections=["C1"], tags=[{"tag": "a", "type": 1}],
                 relations={}, title="Master"),
        "M2": _i("M2", 101, "journalArticle", collections=["C2"], tags=[{"tag": "b", "type": 1}],
                 relations={}, title="Dup"),
        "N1": _i("N1", 103, "note", parent="M2", note="n"),
    }


def _orphan_after_mid_trash_crash(lib, prov):
    """Simulate: snapshot -> merge PATCH -> intent logged -> M2 trashed -> CRASH (no result record)."""
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=prov)
    merge_cluster(snap, lib, lib, library_id=LIB)
    prov.record(activity="commit_merge_intent", item_key="M1", snapshot_id=snap.snapshot_id,
                agent="merge-engine", params={"secondaries": ["M2"]})
    lib.items["M2"]["data"]["deleted"] = 1
    return snap


# ── REC-003 / REC-005: a failed recovery stays failed, discoverable, and blocking ──


def test_failed_reconcile_writes_failure_record_not_success_marker(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    _orphan_after_mid_trash_crash(lib, prov)
    gw = FailingUntrashGateway(lib, fail_keys={"M2"})     # un-trash of M2 will fail

    outcomes = reconcile_orphan_commits(prov, lib, gw, library_id=LIB)
    assert outcomes and outcomes[0]["status"] == "rollback_failed"

    acts = [r["activity"] for r in prov.all_records()]
    assert "commit_merge_reconcile_failed" in acts
    assert "commit_merge_reconciled" not in acts, \
        "REC-005: a success marker must never be written for a failed recovery"


def test_failed_reconcile_remains_in_every_later_scan(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    _orphan_after_mid_trash_crash(lib, prov)
    gw = FailingUntrashGateway(lib, fail_keys={"M2"})

    reconcile_orphan_commits(prov, lib, gw, library_id=LIB)
    # A later scan — including one from a fresh store handle (restart reconstruction) — still sees it.
    assert find_orphan_commit_intents(prov), "REC-003: failed recovery must stay discoverable"
    prov2 = ProvenanceStore(prov.root)                    # simulated restart: state is derived from disk
    assert find_orphan_commit_intents(prov2)
    unresolved = unresolved_transactions(prov2)
    assert unresolved and unresolved[0]["kind"] == "orphan_commit_intent"

    # And a later attempt with a WORKING gateway truly resolves it.
    outcomes = reconcile_orphan_commits(prov2, lib, lib, library_id=LIB)
    assert outcomes[0]["status"] == "reconciled"
    assert not find_orphan_commit_intents(prov2)
    assert unresolved_transactions(prov2) == []


def test_successful_reconcile_resolves_and_untrashes(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    _orphan_after_mid_trash_crash(lib, prov)

    outcomes = reconcile_orphan_commits(prov, lib, lib, library_id=LIB)
    assert outcomes[0]["status"] == "reconciled"
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)
    assert not find_orphan_commit_intents(prov)
    assert unresolved_transactions(prov) == []


# ── REC-004: per-orphan isolation ───────────────────────────────────────────────


def test_one_corrupt_orphan_does_not_hide_the_next(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")

    # Orphan 1: intent whose snapshot blob exists but holds garbage -> reconstruction raises.
    bad_blob = prov.put_json_blob({"garbage": True})
    rec = prov.record(activity="snapshot_cluster", item_key="MX", snapshot_id="SID-BAD")
    # Rewrite is not possible (append-only), so record a second snapshot_cluster row whose
    # entity carries the garbage blob under the same snapshot_id via the public API:
    prov.record(activity="snapshot_cluster", item_key="MX", snapshot_id="SID-BAD",
                before={"garbage": True})
    prov.record(activity="commit_merge_intent", item_key="MX", snapshot_id="SID-BAD",
                agent="merge-engine", params={})
    assert bad_blob and rec  # silence unused warnings

    # Orphan 2: a real recoverable mid-trash crash.
    _orphan_after_mid_trash_crash(lib, prov)

    outcomes = reconcile_orphan_commits(prov, lib, lib, library_id=LIB)
    statuses = {o["snapshot_id"]: o["status"] for o in outcomes}
    assert statuses["SID-BAD"] == "error", "the corrupt orphan is reported, not raised"
    assert [s for sid, s in statuses.items() if sid != "SID-BAD"] == ["reconciled"], \
        "REC-004: the orphan after the corrupt one must still be processed"
    # The corrupt orphan stays unresolved and blocking.
    assert any(u["snapshot_id"] == "SID-BAD" for u in unresolved_transactions(prov))


# ── CON-003 in recovery: never snapshot-revert over an unexplained master state ─


def test_reconcile_preserves_concurrent_master_edit_exactly(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    _orphan_after_mid_trash_crash(lib, prov)
    # A concurrent USER edit lands on the master after the crash: title replaced.
    lib.update_item(LIB, "M1", {"title": "USER EDIT DO NOT LOSE"}, lib.items["M1"]["version"])

    outcomes = reconcile_orphan_commits(prov, lib, lib, library_id=LIB)
    assert outcomes[0]["status"] == "rollback_failed"
    (fail,) = outcomes[0]["rollback"].failures
    assert fail["op"] == "revert-master" and "human recovery" in fail["error"]
    # The concurrent edit is preserved byte-for-byte; the secondary was still safely un-trashed.
    assert lib.items["M1"]["data"]["title"] == "USER EDIT DO NOT LOSE"
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)
    # And the orphan remains unresolved for human recovery.
    assert find_orphan_commit_intents(prov)


def test_master_inversion_provable_states(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=prov)
    base = "http://zotero.org/users/11056739/items"
    from zotero_write_mcp.merge import build_cluster

    # Unchanged master: provable (nothing to revert).
    ok, why = master_inversion_provable(snap, build_cluster(lib, "M1", ["M2"]), library_base=base)
    assert ok and why == "master-unchanged"

    # Engine-authored merge PATCH: provable (matches the projection).
    merge_cluster(snap, lib, lib, library_id=LIB)
    ok, why = master_inversion_provable(snap, build_cluster(lib, "M1", ["M2"]), library_base=base)
    assert ok and why == "matches-engine-projection"

    # External edit on top: NOT provable.
    lib.update_item(LIB, "M1", {"title": "changed by user"}, lib.items["M1"]["version"])
    ok, why = master_inversion_provable(snap, build_cluster(lib, "M1", ["M2"]), library_base=base)
    assert not ok and why == "unexplained-master-state"


# ── merge_txn_unresolved records participate in the unresolved scan ─────────────


def test_merge_txn_unresolved_blocks_until_resolved(tmp_path):
    prov = ProvenanceStore(tmp_path / "p")
    prov.record(activity="merge_txn_unresolved", item_key="M9",
                params={"transaction_id": "TXN-1"})
    unresolved = unresolved_transactions(prov)
    assert [u["kind"] for u in unresolved] == ["unresolved_transaction"]
    assert unresolved[0]["transaction_id"] == "TXN-1"

    prov.record(activity="merge_txn_resolved", item_key="M9",
                params={"transaction_id": "TXN-1", "note": "owner repaired manually"})
    assert unresolved_transactions(prov) == []


# ── readiness rows (REC-006 / LOG-001) ──────────────────────────────────────────


def test_readiness_blocks_on_unresolved_and_log_damage(tmp_path):
    from zotero_write_mcp import readiness
    from datetime import timedelta
    from zotero_write_mcp.observability import daily_report

    prov = ProvenanceStore(tmp_path / "p")
    now = datetime.now(timezone.utc)
    daily_report(prov, ts=(now - timedelta(hours=1)).isoformat())   # freshness satisfied

    row = readiness.unresolved_transactions_row(prov)
    assert row["status"] == "pass"

    prov.record(activity="merge_txn_unresolved", item_key="M9", params={"transaction_id": "TXN-9"})
    row = readiness.unresolved_transactions_row(prov)
    assert row["status"] == "fail" and "TXN-9" in row["detail"]

    rep = readiness.readiness_report(prov, probe_local_api=False)
    assert rep["live_merge_safe_now"] is False, "REC-006: unresolved state must veto the merge verdict"

    # Log damage independently vetoes the verdict (LOG-001).
    prov2 = ProvenanceStore(tmp_path / "p2")
    daily_report(prov2, ts=(now - timedelta(hours=1)).isoformat())
    with open(prov2.prov_path, "ab") as f:
        f.write(b"damaged-line\n")
    row = readiness.log_integrity_row(prov2)
    assert row["status"] == "fail"
    rep2 = readiness.readiness_report(prov2, probe_local_api=False)
    assert rep2["live_merge_safe_now"] is False

    # Owner acceptance restores the verdict while keeping the damage visible.
    (bad,) = prov2.scan_integrity()["bad_lines"]
    prov2.accept_log_damage(bad["line_no"], bad["line_sha256"], note="inspected; benign crash tail")
    row = readiness.log_integrity_row(prov2)
    assert row["status"] == "pass" and row["integrity_status"] == "accepted_damage"
