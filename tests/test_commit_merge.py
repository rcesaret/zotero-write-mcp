"""Unit tests for commit_merge (P2-commit-merge) — the verify-gated, fail-closed trash.

A shared mutable FakeLibrary serves as BOTH the ClusterReader and the gateway: update_item mutates
state so re-reads reflect reparents/trashes (simulating the live Web API, incl. trashed children, which
the reader contract requires for M-4 / the terminal verify). Covers every gate + failure path."""
from datetime import datetime, timezone

import pytest

from zotero_write_mcp.gateway import ConcurrencyConflictError
from zotero_write_mcp.merge import snapshot_cluster
from zotero_write_mcp.merge_live import merge_cluster, commit_merge, ENABLE_ENV, ENABLE_TOKEN
from zotero_write_mcp.observability import daily_report
from zotero_write_mcp.provenance import ProvenanceStore

NOW = datetime(2026, 6, 24, 12, 30, tzinfo=timezone.utc)
FRESH_TS = "2026-06-24T12:00:00+00:00"


def _i(key, version, itype, parent=None, **extra):
    data = {"key": key, "version": version, "itemType": itype, **extra}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": version, "data": data}


class FakeLibrary:
    """Mutable library; reader + gateway in one. get_children/get_annotations include trashed items."""

    def __init__(self, items):
        self.items = items
        self.lib_ver = max(it["version"] for it in items.values())

    # ---- ClusterReader ----
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

    # ---- gateway ----
    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        it = self.items[item_key]
        self.lib_ver += 1
        it["data"].update(data)
        it["version"] = self.lib_ver
        it["data"]["version"] = self.lib_ver

    def create_items(self, library_id, objects, *, library_type="user"):
        raise AssertionError("create_items must NOT be called on the trash-not-purge path")


def make_raw():
    """M1 master (+ own attachment A0) ; M2 secondary (note N1 + attachment A1 with annotation AN1)."""
    return {
        "M1": _i("M1", 100, "journalArticle", collections=["C1"], tags=[{"tag": "a", "type": 1}],
                 relations={}, title="Master"),
        "M2": _i("M2", 101, "journalArticle", collections=["C2"], tags=[{"tag": "b", "type": 1}],
                 relations={}, title="Dup"),
        "A0": _i("A0", 102, "attachment", parent="M1", md5="x", filename="a.pdf"),
        "N1": _i("N1", 103, "note", parent="M2", note="n"),
        "A1": _i("A1", 104, "attachment", parent="M2", md5="y", filename="b.pdf"),
        "AN1": _i("AN1", 105, "annotation", parent="A1"),
    }


def _snap_and_merge(lib, tmp_path):
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=ProvenanceStore(tmp_path / "snap"))
    merge_cluster(snap, lib, lib, library_id=11056739)          # applies the PATCH phase to lib
    return snap


def _fresh_prov(tmp_path):
    prov = ProvenanceStore(tmp_path / "prov")
    daily_report(prov, ts=FRESH_TS)                            # observability is fresh
    return prov


# ── default SHADOW (no enable token): verify runs + logs, NO trash ─────────────

def test_shadow_when_not_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    lib = FakeLibrary(make_raw())
    snap = _snap_and_merge(lib, tmp_path)
    prov = _fresh_prov(tmp_path)
    res = commit_merge(snap, lib, lib, prov, library_id=11056739, now=NOW)
    assert res.mode == "shadow" and res.verify_passed is True
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)         # NOT trashed
    assert "commit_merge_shadow" in {r["activity"] for r in prov.all_records()}
    assert "commit_merge" not in {r["activity"] for r in prov.all_records()}


# ── ceiling (H-1) ──────────────────────────────────────────────────────────────

def test_ceiling_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    raw = {"M1": _i("M1", 1, "journalArticle", collections=[], tags=[], relations={}, title="t")}
    for i, k in enumerate(["S1", "S2", "S3"]):
        raw[k] = _i(k, 2 + i, "journalArticle", collections=[], tags=[], relations={}, title="t")
    lib = FakeLibrary(raw)
    snap = snapshot_cluster(lib, "M1", ["S1", "S2", "S3"], prov=ProvenanceStore(tmp_path / "s"))
    res = commit_merge(snap, lib, lib, ProvenanceStore(tmp_path / "p"),
                       library_id=11056739, ceiling=2, now=NOW)
    assert res.mode == "blocked" and "ceiling" in res.reason


# ── verify FAIL after PATCH (M-3) -> rollback ──────────────────────────────────

def test_verify_fail_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = FakeLibrary(make_raw())
    snap = _snap_and_merge(lib, tmp_path)
    lib.items["M1"]["data"]["collections"] = ["C1"]            # corrupt: dropped C2 from the union
    res = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
    assert res.mode == "rolled_back" and res.verify_passed is False
    assert "collections-equality" in res.reason
    assert res.rollback.state == "b"                           # PATCHed-not-trashed
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)  # nothing trashed
    assert lib.items["N1"]["data"]["parentItem"] == "M2"        # child reparented back


# ── enabled but observability STALE/absent (C-2) -> blocked, no trash ──────────

def test_blocked_when_observability_stale(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = FakeLibrary(make_raw())
    snap = _snap_and_merge(lib, tmp_path)
    prov = ProvenanceStore(tmp_path / "prov")                  # NO daily_report
    res = commit_merge(snap, lib, lib, prov, library_id=11056739, now=NOW)
    assert res.mode == "blocked" and "observability" in res.reason
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)


# ── happy path: enabled + fresh -> verify -> TRASH -> commit ───────────────────

def test_committed_trashes_not_purges(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = FakeLibrary(make_raw())
    snap = _snap_and_merge(lib, tmp_path)
    prov = _fresh_prov(tmp_path)
    res = commit_merge(snap, lib, lib, prov, library_id=11056739, now=NOW)
    assert res.mode == "committed" and res.trashed == ["M2"]
    assert lib.items["M2"]["data"]["deleted"] == 1            # TRASHED (deleted flag), not purged
    assert "M2" in lib.items                                  # still present -> recoverable
    assert lib.items["N1"]["data"]["parentItem"] == "M1" and lib.items["N1"]["data"].get("deleted") in (None, 0)
    acts = [r["activity"] for r in prov.all_records()]
    assert "commit_merge_intent" in acts and "commit_merge" in acts   # two-phase PROV (C-3)
    # the result record carries before/after blobs for the sampled audit
    result = next(r for r in prov.all_records() if r["activity"] == "commit_merge")
    assert result["entity"]["before_blob"] and result["entity"]["after_blob"]


def test_token_must_match_exactly(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "almost-but-not-the-token")
    lib = FakeLibrary(make_raw())
    snap = _snap_and_merge(lib, tmp_path)
    res = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
    assert res.mode == "shadow"                               # wrong token -> shadow, never trash


# ── M-5: partial trash (412 mid-batch) -> rollback (untrash the done subset) ───

class TrashFailLibrary(FakeLibrary):
    def __init__(self, items, fail_key):
        super().__init__(items)
        self.fail_key = fail_key

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        if data.get("deleted") == 1 and item_key == self.fail_key:
            raise ConcurrencyConflictError(f"412 trashing {item_key}")
        return super().update_item(library_id, item_key, data, version, library_type=library_type)


class TrashErrorLibrary(FakeLibrary):
    """Raises a NON-412 GatewayError on a designated secondary's trash (a transient 5xx)."""

    def __init__(self, items, fail_key):
        super().__init__(items)
        self.fail_key = fail_key

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        if data.get("deleted") == 1 and item_key == self.fail_key:
            from zotero_write_mcp.gateway import GatewayError
            raise GatewayError(f"unexpected HTTP 503 trashing {item_key}")
        return super().update_item(library_id, item_key, data, version, library_type=library_type)


def test_f1_non_412_trash_failure_rolls_back(tmp_path, monkeypatch):
    """F1 BLOCKER: a NON-412 GatewayError (transient 5xx) mid-trash must route to rollback, not escape."""
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    raw = {"M1": _i("M1", 1, "journalArticle", collections=["C1"], tags=[], relations={}, title="t"),
           "S1": _i("S1", 2, "journalArticle", collections=["C2"], tags=[], relations={}, title="t"),
           "S2": _i("S2", 3, "journalArticle", collections=["C3"], tags=[], relations={}, title="t")}
    lib = TrashErrorLibrary(raw, fail_key="S2")
    snap = snapshot_cluster(lib, "M1", ["S1", "S2"], prov=ProvenanceStore(tmp_path / "s"))
    merge_cluster(snap, lib, lib, library_id=11056739)
    res = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
    assert res.mode == "rolled_back"                              # not an uncaught exception
    assert res.trashed == ["S1"]                                  # S1 trashed before S2's 503
    assert lib.items["S1"]["data"].get("deleted") in (None, 0)    # rollback un-trashed S1


def test_m5_partial_trash_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    raw = {"M1": _i("M1", 1, "journalArticle", collections=["C1"], tags=[], relations={}, title="t"),
           "S1": _i("S1", 2, "journalArticle", collections=["C2"], tags=[], relations={}, title="t"),
           "S2": _i("S2", 3, "journalArticle", collections=["C3"], tags=[], relations={}, title="t")}
    lib = TrashFailLibrary(raw, fail_key="S2")
    snap = snapshot_cluster(lib, "M1", ["S1", "S2"], prov=ProvenanceStore(tmp_path / "s"))
    merge_cluster(snap, lib, lib, library_id=11056739)
    prov = _fresh_prov(tmp_path)
    res = commit_merge(snap, lib, lib, prov, library_id=11056739, now=NOW)
    assert res.mode == "rolled_back" and "412" in res.reason
    assert res.trashed == ["S1"]                             # S1 trashed before S2 failed
    assert res.rollback.state == "c"                         # partial-DELETE state
    assert lib.items["S1"]["data"].get("deleted") in (None, 0)   # S1 un-trashed by rollback
    assert "commit_merge_intent" in {r["activity"] for r in prov.all_records()}   # intent recorded (C-3)


# ── M-4: a child cascade-trashed by the secondary trash is re-asserted live ────

class CascadeLibrary(FakeLibrary):
    """Trashing M2 also cascade-trashes a designated (already-reparented) child once."""

    def __init__(self, items, cascade_child):
        super().__init__(items)
        self.cascade_child = cascade_child
        self._done = False

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        super().update_item(library_id, item_key, data, version, library_type=library_type)
        if data.get("deleted") == 1 and item_key == "M2" and not self._done:
            self._done = True
            self.items[self.cascade_child]["data"]["deleted"] = 1     # cascade


def test_m4_cascade_child_reasserted(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = CascadeLibrary(make_raw(), cascade_child="N1")
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=ProvenanceStore(tmp_path / "s"))
    merge_cluster(snap, lib, lib, library_id=11056739)        # N1, A1 reparent to M1
    res = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
    assert res.mode == "committed"
    assert lib.items["M2"]["data"]["deleted"] == 1            # secondary trashed
    assert lib.items["N1"]["data"].get("deleted") in (None, 0)  # M-4 re-asserted the cascade-trashed child
    assert lib.items["N1"]["data"]["parentItem"] == "M1"


# ── F5: a concurrent master edit during the trash window is caught by the terminal verify ───

class MasterEditLibrary(FakeLibrary):
    """When M2 is trashed, an external actor concurrently drops M1 from a collection (the union)."""

    def __init__(self, items):
        super().__init__(items)
        self._done = False

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        super().update_item(library_id, item_key, data, version, library_type=library_type)
        if data.get("deleted") == 1 and item_key == "M2" and not self._done:
            self._done = True
            self.items["M1"]["data"]["collections"] = ["C1"]        # drop the unioned C2 mid-commit


def test_f5_concurrent_master_edit_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = MasterEditLibrary(make_raw())
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=ProvenanceStore(tmp_path / "s"))
    merge_cluster(snap, lib, lib, library_id=11056739)
    res = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
    assert res.mode in ("rolled_back", "rollback_failed")
    assert "master-collections" in res.reason                       # terminal verify caught the master edit


# ── M-4-disjoint: overlapping in-flight cluster is serialized ───

def test_m4_disjoint_blocks_overlapping_inflight(tmp_path, monkeypatch):
    from zotero_write_mcp.merge_live import _acquire_cluster, _release_cluster
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = FakeLibrary(make_raw())
    snap = _snap_and_merge(lib, tmp_path)
    _acquire_cluster(["M2"])                                        # another in-flight commit holds M2
    try:
        res = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
        assert res.mode == "blocked" and "disjoint" in res.reason
    finally:
        _release_cluster(["M2"])
    res2 = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
    assert res2.mode == "committed"                                  # proceeds once the claim is released


# ── R-3: a rollback whose own op fails escalates to mode='rollback_failed' ───

class RevertFailLibrary(FakeLibrary):
    """Fails the rollback's revert-master PATCH (which carries scalar fields like 'title')."""

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        if "title" in data and item_key == "M1":      # only rollback's revert-master carries title
            raise ConcurrencyConflictError("revert-master 412")
        return super().update_item(library_id, item_key, data, version, library_type=library_type)


def test_r3_rollback_failure_escalates(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = RevertFailLibrary(make_raw())
    snap = _snap_and_merge(lib, tmp_path)
    lib.items["M1"]["data"]["collections"] = ["C1"]    # corrupt -> pre-trash verify fails -> rollback (revert-master)
    res = commit_merge(snap, lib, lib, _fresh_prov(tmp_path), library_id=11056739, now=NOW)
    assert res.mode == "rollback_failed"               # not an uncaught exception, not a clean rollback
    assert res.rollback is not None and res.rollback.ok is False and res.rollback.failures
