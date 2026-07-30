"""Routine Supervised v1.0 — the production merge transaction (PRD §6; workflow §5.3 regressions).

The FakeLibrary here ENFORCES optimistic concurrency like the real Web API: an update whose
version does not match the item's current version raises ConcurrencyConflictError when
retry_on_412=False. That makes the concurrency regressions production-shaped rather than
permissive-fake results.
"""
import inspect

import pytest

from zotero_write_mcp.gateway import ConcurrencyConflictError
from zotero_write_mcp.merge_live import ENABLE_ENV, ENABLE_TOKEN, unresolved_transactions
from zotero_write_mcp.merge_txn import (
    TXN_BLOCKED, TXN_INTENT, TXN_RESULT, TXN_SHADOW, TXN_UNRESOLVED, TXN_WRITE,
    execute_merge_txn, propose_merge_txn, reconcile_orphan_txns,
)
from zotero_write_mcp.provenance import ProvenanceStore

LIB = 11056739


def _i(key, version, itype, parent=None, **extra):
    data = {"key": key, "version": version, "itemType": itype, **extra}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": version, "data": data}


class FakeLibrary:
    """Reader + version-ENFORCING gateway. update_item raises ConcurrencyConflictError on a stale
    version when retry_on_412=False (the only mode the transaction layer uses)."""

    def __init__(self, items):
        self.items = items
        self.lib_ver = max(it["version"] for it in items.values())
        self.write_log: list = []
        self.after_write_hook = None      # callable(op_index) -> None, for injecting concurrency

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
        data = self.items[key]["data"]
        extra = data.get("extra") or ""
        for ln in extra.splitlines():
            if ln.strip().lower().startswith("citation key:"):
                return ln.split(":", 1)[1].strip()
        return data.get("citationKey")

    # ---- gateway ----
    def update_item(self, library_id, item_key, data, version, *, library_type="user",
                    retry_on_412=True):
        it = self.items[item_key]
        if version != it["version"]:
            raise ConcurrencyConflictError(
                f"update_item({item_key}) 412: version {version} != current {it['version']}")
        self.lib_ver += 1
        it["data"].update(data)
        it["version"] = self.lib_ver
        it["data"]["version"] = self.lib_ver
        self.write_log.append({"key": item_key, "data": dict(data)})
        if self.after_write_hook:
            self.after_write_hook(len(self.write_log))

    def external_edit(self, key, **data):
        """A concurrent USER edit: bumps the version outside the engine's write path."""
        it = self.items[key]
        self.lib_ver += 1
        it["data"].update(data)
        it["version"] = self.lib_ver
        it["data"]["version"] = self.lib_ver

    def create_items(self, *a, **k):
        raise AssertionError("create_items must NOT be called (trash-not-purge)")

    def delete_items(self, *a, **k):
        raise AssertionError("delete/purge must NEVER be called by the v1 transaction (MRG-006)")


def make_raw():
    return {
        "M1": _i("M1", 100, "journalArticle", collections=["C1"], tags=[{"tag": "a", "type": 1}],
                 relations={}, title="Master", extra="Citation Key: sandersBasin1979"),
        "M2": _i("M2", 101, "journalArticle", collections=["C2"], tags=[{"tag": "b", "type": 1}],
                 relations={}, title="Dup", citationKey="dupKey2001"),
        "N1": _i("N1", 103, "note", parent="M2", note="n"),
        "A1": _i("A1", 104, "attachment", parent="M2", md5="y", filename="b.pdf"),
    }


def _propose(lib, prov):
    return propose_merge_txn(lib, prov, "M1", ["M2"], library_id=LIB)


def _execute(tid, lib, prov):
    return execute_merge_txn(tid, lib, lib, prov, library_id=LIB)


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)


@pytest.fixture()
def shadow(monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)


# ── Interface: the production surface structurally lacks the dangerous inputs ──


def test_execute_surface_has_no_enrichment_or_version_params():
    """MRG-001/002 + CON-001: smart_fill, field_sources, and any version/expected_version parameter
    are ABSENT from the production transaction signature — they cannot be passed at all."""
    params = set(inspect.signature(execute_merge_txn).parameters)
    assert "smart_fill" not in params
    assert "field_sources" not in params
    assert not any("version" in p for p in params)
    propose_params = set(inspect.signature(propose_merge_txn).parameters)
    assert "smart_fill" not in propose_params
    assert "field_sources" not in propose_params


# ── Proposal binding (SCOPE-003, PRV-001/002) ──────────────────────────────────


def test_proposal_id_is_content_bound(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    p1 = _propose(lib, prov)
    p2 = _propose(lib, prov)
    assert p1["transaction_id"] == p2["transaction_id"], "same state -> same id (idempotent proposal)"
    lib.external_edit("M2", title="changed")
    p3 = _propose(lib, prov)
    assert p3["transaction_id"] != p1["transaction_id"], "changed member version -> different id"


def test_proposal_is_labeled_non_authoritative(tmp_path):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    p = _propose(lib, prov)
    assert p["verified_against"] == "projection"
    assert p["gate_authoritative"] is False
    assert "PREVIEW ONLY" in p["warning"]
    assert p["members"]["M1"]["role"] == "survivor"
    assert p["members"]["M2"]["role"] == "secondary"


def test_stale_proposal_blocks_before_write(tmp_path, live):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    lib.external_edit("M1", title="edited after approval")
    res = _execute(tid, lib, prov)
    assert res.state == "blocked_before_write"
    assert "stale" in res.reason
    assert lib.write_log == [], "no engine write may occur on a stale proposal"


def test_unknown_transaction_blocks(tmp_path, live):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    res = _execute("TXN-doesnotexist", lib, prov)
    assert res.state == "blocked_before_write" and "unknown" in res.reason
    assert lib.write_log == []


def test_trashed_member_blocks_before_write(tmp_path, live):
    lib = FakeLibrary(make_raw())
    lib.items["M2"]["data"]["deleted"] = 1
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    res = _execute(tid, lib, prov)
    assert res.state == "blocked_before_write" and "trash" in res.reason
    assert lib.write_log == []


# ── Shadow (MUT-002) ───────────────────────────────────────────────────────────


def test_shadow_performs_no_library_mutation(tmp_path, shadow):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    res = _execute(tid, lib, prov)
    assert res.state == "shadow"
    assert res.live_write_performed is False
    assert lib.write_log == []
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)
    acts = [r["activity"] for r in prov.all_records()]
    assert TXN_SHADOW in acts and TXN_INTENT not in acts
    assert res.verify["gate_authoritative"] is False


# ── Committed happy path (MRG-005/006/007/008, MRG-004) ────────────────────────


def test_committed_merge_full_contract(tmp_path, live):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    res = _execute(tid, lib, prov)
    assert res.state == "committed"
    assert res.verify["pass"] is True and res.verify["verified_against"] == "observed"
    assert res.terminal["pass"] is True

    # Secondaries trashed (present with deleted=1), never purged (delete_items would have raised).
    assert lib.items["M2"]["data"]["deleted"] == 1
    # Children live and reparented to the survivor.
    assert lib.items["N1"]["data"]["parentItem"] == "M1"
    assert lib.items["A1"]["data"]["parentItem"] == "M1"
    # Conservative projection: unions + dc:replaces; citekey identity preserved; alias accumulated.
    m1 = lib.items["M1"]["data"]
    assert set(m1["collections"]) == {"C1", "C2"}
    assert any("M2" in v for v in m1["relations"]["dc:replaces"])
    assert "Citation Key: sandersBasin1979" in m1["extra"]
    assert "dupKey2001" in m1["extra"], "duplicate's citekey preserved as a tex.ids alias"
    # No scalar enrichment: the survivor's own title stands.
    assert m1["title"] == "Master"
    # Durable evidence chain: intent BEFORE writes, receipts, result (REC-001, LOG-004).
    acts = [r["activity"] for r in prov.all_records()]
    assert acts.index(TXN_INTENT) < acts.index(TXN_WRITE)
    assert TXN_RESULT in acts
    assert unresolved_transactions(prov) == []


# ── Idempotency (IDEM-001/002) ─────────────────────────────────────────────────


def test_retry_of_committed_txn_performs_no_second_mutation(tmp_path, live):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    assert _execute(tid, lib, prov).state == "committed"
    n_writes = len(lib.write_log)
    res2 = _execute(tid, lib, prov)
    assert res2.state == "already_committed"
    assert len(lib.write_log) == n_writes, "IDEM-001: retry must not repeat destructive work"
    assert res2.trashed == ["M2"]


# ── Concurrency: conflict means stop, not overwrite (CON-001/002/003/004) ─────


def test_concurrent_master_edit_before_patch_blocks_cleanly(tmp_path, live):
    """The proposal is fresh but an edit lands in the propose->execute gap: the fresh-read version
    check catches it BEFORE any write (blocked_before_write, nothing mutated)."""
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    lib.external_edit("M1", abstractNote="added concurrently")     # additive edit
    res = _execute(tid, lib, prov)
    assert res.state == "blocked_before_write"
    assert lib.items["M1"]["data"]["abstractNote"] == "added concurrently"
    assert lib.write_log == []


def test_concurrent_additive_edit_after_patch_stops_without_trash(tmp_path, live):
    """§5.3 #7: an ADDITIVE concurrent edit lands after the master PATCH. The engine's version pin
    detects it before any trash; inversion stops at the unexplained version; the edit survives
    byte-for-byte; the transaction is unresolved and blocks."""
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]

    def inject(op_index):
        if op_index == 3:      # after patch-master + 2 reparents, before the pre-trash re-read
            lib.after_write_hook = None
            lib.external_edit("M1", abstractNote="concurrent addition")
    lib.after_write_hook = inject

    res = _execute(tid, lib, prov)
    assert res.state == "unresolved"
    assert lib.items["M2"]["data"].get("deleted") in (None, 0), "no secondary trash after conflict"
    assert res.trashed == []
    assert lib.items["M1"]["data"]["abstractNote"] == "concurrent addition"
    assert unresolved_transactions(prov), "unresolved state must be visible and blocking"
    # And it blocks the next transaction (REC-006).
    tid2 = _propose(lib, prov)["transaction_id"]
    res2 = _execute(tid2, lib, prov)
    assert res2.state == "blocked_before_write" and "unresolved" in res2.reason


def test_concurrent_replacement_edit_after_patch_preserved_exactly(tmp_path, live):
    """§5.3 #8: a REPLACEMENT concurrent edit (title overwritten) is preserved exactly."""
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]

    def inject(op_index):
        if op_index == 3:
            lib.after_write_hook = None
            lib.external_edit("M1", title="REPLACED BY USER")
    lib.after_write_hook = inject

    res = _execute(tid, lib, prov)
    assert res.state == "unresolved"
    assert lib.items["M1"]["data"]["title"] == "REPLACED BY USER"
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)


def test_concurrent_child_edit_detected_without_overwrite(tmp_path, live):
    """§5.3 #9: a child edited between proposal and execution changes the child's version; the
    reparent write 412s, the transaction stops, and the child's concurrent state survives."""
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]

    def inject(op_index):
        if op_index == 1:      # after patch-master, before the reparents
            lib.after_write_hook = None
            lib.external_edit("N1", note="edited concurrently")
    lib.after_write_hook = inject

    res = _execute(tid, lib, prov)
    assert res.state in ("rolled_back", "unresolved")
    assert lib.items["N1"]["data"]["note"] == "edited concurrently"
    assert lib.items["N1"]["data"]["parentItem"] == "M2", "child not force-reparented over the edit"
    assert lib.items["M2"]["data"].get("deleted") in (None, 0), "no trash after child conflict"


def test_partial_trash_conflict_rolls_back_completely(tmp_path, live):
    """§5.3 partial-commit: with two secondaries, the second trash 412s (concurrent edit). The
    engine inverts its own writes — first secondary un-trashed, master and children restored —
    and the library returns to its pre-transaction state."""
    raw = make_raw()
    raw["M3"] = _i("M3", 105, "journalArticle", collections=[], tags=[], relations={}, title="Dup2")
    lib = FakeLibrary(raw)
    prov = ProvenanceStore(tmp_path / "p")
    p = propose_merge_txn(lib, prov, "M1", ["M2", "M3"], library_id=LIB)

    def inject(op_index):
        # write order: patch-master, reparent N1, reparent A1, trash M2, then M3 next -> edit M3 now
        if op_index == 4:
            lib.after_write_hook = None
            lib.external_edit("M3", title="edited during trash phase")
    lib.after_write_hook = inject

    res = execute_merge_txn(p["transaction_id"], lib, lib, prov, library_id=LIB)
    assert res.state == "rolled_back"
    assert lib.items["M2"]["data"].get("deleted") in (None, 0), "trashed secondary restored"
    assert lib.items["M3"]["data"]["title"] == "edited during trash phase"
    assert lib.items["N1"]["data"]["parentItem"] == "M2", "children restored to original parents"
    m1 = lib.items["M1"]["data"]
    assert set(m1["collections"]) == {"C1"}, "master unions reverted"
    assert unresolved_transactions(prov) == [], "a complete rollback is a resolved terminal state"


# ── Terminal guard (§5.3 #16 / MRG-007) ────────────────────────────────────────


def test_trashed_survivor_cannot_produce_committed(tmp_path, live):
    """A concurrent trash of the SURVIVOR during the trash window must never end 'committed'."""
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]

    def inject(op_index):
        if op_index == 4:      # after the trash of M2 (last write before re-reads)
            lib.after_write_hook = None
            lib.external_edit("M1", deleted=1)
    lib.after_write_hook = inject

    res = _execute(tid, lib, prov)
    assert res.state != "committed"
    assert res.state == "unresolved"       # master version unexplained -> non-destructive stop
    assert unresolved_transactions(prov)


# ── Gate 0: log integrity and ceiling ──────────────────────────────────────────


def test_damaged_log_blocks_execution(tmp_path, live):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    with open(prov.prov_path, "ab") as f:
        f.write(b"damaged\n")
    res = _execute(tid, lib, prov)
    assert res.state == "blocked_before_write" and "integrity" in res.reason
    assert lib.write_log == []


def test_ceiling_blocks(tmp_path, live):
    raw = {"M1": _i("M1", 1, "journalArticle", collections=[], tags=[], relations={}, title="t")}
    secs = []
    for i in range(12):
        k = f"S{i}"
        raw[k] = _i(k, 2 + i, "journalArticle", collections=[], tags=[], relations={}, title="t")
        secs.append(k)
    lib = FakeLibrary(raw)
    prov = ProvenanceStore(tmp_path / "p")
    p = propose_merge_txn(lib, prov, "M1", secs, library_id=LIB)
    res = execute_merge_txn(p["transaction_id"], lib, lib, prov, library_id=LIB)
    assert res.state == "blocked_before_write" and "ceiling" in res.reason


# ── Crash recovery (REC-001/003; receipts-based) ───────────────────────────────


def _crash_mid_trash(tmp_path, lib, prov, monkeypatch):
    """Run a real transaction but crash it right after the first trash write by raising from the
    hook — the engine's own exception handler is bypassed by BaseException to simulate a hard kill.
    Actually: we simulate the crash by snapshotting PROV state mid-flight via a hook that raises
    KeyboardInterrupt (not caught by `except Exception`)."""
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    tid = _propose(lib, prov)["transaction_id"]

    def inject(op_index):
        if op_index == 4:      # right after trash M2 landed
            raise KeyboardInterrupt("simulated process death")
    lib.after_write_hook = inject
    with pytest.raises(KeyboardInterrupt):
        _execute(tid, lib, prov)
    lib.after_write_hook = None
    return tid


def test_crash_mid_trash_is_discoverable_and_recoverable(tmp_path, monkeypatch):
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _crash_mid_trash(tmp_path, lib, prov, monkeypatch)

    # REC-001: the orphan is locatable from durable state alone (fresh store handle = restart).
    prov2 = ProvenanceStore(prov.root)
    unresolved = unresolved_transactions(prov2)
    assert any(u.get("transaction_id") == tid for u in unresolved)
    assert lib.items["M2"]["data"].get("deleted") == 1, "mid-trash crash left M2 trashed"

    # Recovery inverts the receipts: M2 un-trashed, master and children restored.
    outcomes = reconcile_orphan_txns(prov2, lib, lib, library_id=LIB)
    assert outcomes and outcomes[0]["status"] == "rolled_back"
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)
    assert lib.items["N1"]["data"]["parentItem"] == "M2"
    assert set(lib.items["M1"]["data"]["collections"]) == {"C1"}
    assert unresolved_transactions(prov2) == []


def test_crash_recovery_with_concurrent_edit_stays_unresolved(tmp_path, monkeypatch):
    """§5.3 #10: recovery that CANNOT prove exclusivity stays unresolved on every later scan."""
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _crash_mid_trash(tmp_path, lib, prov, monkeypatch)
    lib.external_edit("M1", title="user edited before recovery ran")

    prov2 = ProvenanceStore(prov.root)
    outcomes = reconcile_orphan_txns(prov2, lib, lib, library_id=LIB)
    assert outcomes[0]["status"] == "unresolved"
    assert lib.items["M1"]["data"]["title"] == "user edited before recovery ran"
    # Still unresolved after another restart + rescan (REC-003).
    prov3 = ProvenanceStore(prov.root)
    assert any(u.get("transaction_id") == tid for u in unresolved_transactions(prov3))
    acts = [r["activity"] for r in prov3.all_records()]
    assert TXN_UNRESOLVED in acts


def test_no_purge_path_in_transaction_source():
    """§5.3 #14 static half: the transaction layer contains no delete/purge call at all — trash is
    the only removal verb (PATCH deleted:1). The dynamic half is enforced in every test above by
    FakeLibrary.delete_items raising."""
    import pathlib
    import zotero_write_mcp.merge_txn as mt
    src = pathlib.Path(mt.__file__).read_text(encoding="utf-8")
    assert "delete_items" not in src
    assert '"deleted": 1' in src


def test_blocked_txn_is_not_an_orphan(tmp_path, live):
    """A blocked_before_write terminal record resolves the intent bookkeeping — it must not linger
    as a phantom unresolved transaction."""
    lib = FakeLibrary(make_raw())
    prov = ProvenanceStore(tmp_path / "p")
    tid = _propose(lib, prov)["transaction_id"]
    lib.external_edit("M2", title="stale-maker")
    res = _execute(tid, lib, prov)
    assert res.state == "blocked_before_write"
    assert unresolved_transactions(prov) == []
    acts = [r["activity"] for r in prov.all_records()]
    assert TXN_BLOCKED in acts
