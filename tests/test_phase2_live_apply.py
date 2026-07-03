"""Unit tests for scripts/phase2_live_apply.py — the LIVE gated apply driver.

Two layers, both offline (no live library, no token left set):

  * ORCHESTRATION (against a stub ``commit_one``): checkpoint-skip, first-cluster-stop, fail-stop,
    token-refusal, and the #52 / uncertain exclusion — the driver's own control flow, isolated from the
    engine chain (which is exhaustively covered by test_commit_merge.py).
  * REAL CHAIN (against the same mutable FakeLibrary test_commit_merge.py uses as reader+gateway): proves
    ``live_commit_one`` actually wires snapshot -> merge_cluster(field_sources, capture master_version) ->
    commit_merge(field_sources, expected_master_version) and that ``run`` writes the checkpoint + trashes
    (not purges) for real — i.e. the driver is NOT only stub-tested.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import phase2_live_apply as D  # noqa: E402
from zotero_write_mcp.merge_live import ENABLE_ENV, ENABLE_TOKEN  # noqa: E402
from zotero_write_mcp.observability import daily_report  # noqa: E402
from zotero_write_mcp.provenance import ProvenanceStore  # noqa: E402

LIB = 11056739
NOW = datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc)
FRESH_TS = "2026-07-03T12:00:00+00:00"
FIXED_CLOCK = lambda: NOW  # noqa: E731


# ── helpers ─────────────────────────────────────────────────────────────────────────
def job(master, secondaries, is_dup="yes", members=None, index=1, field_sources=None):
    members = members if members is not None else [master, *secondaries]
    return D.ClusterJob(index=index, master=master, secondaries=list(secondaries),
                        member_keys=sorted(members), is_duplicate=is_dup, field_sources=field_sources or {})


def make_stub(modes):
    """A commit_one stub returning CommitResult(mode) in order; records the masters it was called on."""
    seq = iter(modes)
    calls = []

    def stub(j, reader, gateway, prov, *, library_id, now):
        calls.append(j.master)
        mode = next(seq)
        res = D.CommitResult(mode=mode, verify_passed=(mode == "committed"),
                             trashed=(list(j.secondaries) if mode == "committed" else []),
                             reason=("" if mode == "committed" else f"stub {mode}"),
                             intent_prov_id=f"prov-{j.master}")
        return D.LiveOutcome(res, f"snap-{j.master}")

    stub.calls = calls
    return stub


def _run(jobs, tmp_path, *, commit_one, continue_after_first, checkpoint=None):
    prov = ProvenanceStore(tmp_path / "prov")
    cp = checkpoint or str(tmp_path / "live-checkpoint.jsonl")
    return D.run(jobs, reader=None, gateway=None, prov=prov, mode="live", library_id=LIB,
                 checkpoint_path=cp, continue_after_first=continue_after_first,
                 commit_one=commit_one, clock=FIXED_CLOCK, emit=lambda *a, **k: None), cp


# ── 1. checkpoint-skip (resumable) ───────────────────────────────────────────────────
def test_checkpoint_skip(tmp_path):
    a, b, c = job("A", ["a2"]), job("B", ["b2"], index=2), job("C", ["c2"], index=3)
    cp = str(tmp_path / "live-checkpoint.jsonl")
    D.append_checkpoint(cp, {"member_keys": sorted(["A", "a2"]), "mode": "committed"})  # A already done
    stub = make_stub(["committed", "committed"])
    report, _ = _run([a, b, c], tmp_path, commit_one=stub, continue_after_first=True, checkpoint=cp)
    assert stub.calls == ["B", "C"]                      # A skipped, only B & C processed
    assert report.counts["committed"] == 2
    assert report.counts["skipped_resumed"] == 1


# ── 2. first-cluster stop ────────────────────────────────────────────────────────────
def test_first_cluster_stop(tmp_path):
    jobs = [job("A", ["a2"]), job("B", ["b2"], index=2), job("C", ["c2"], index=3)]
    stub = make_stub(["committed", "committed", "committed"])
    report, _ = _run(jobs, tmp_path, commit_one=stub, continue_after_first=False)
    assert stub.calls == ["A"]                           # halted after the first commit; B/C untouched
    assert report.stopped_after_first is True
    assert report.committed_this_run == 1
    assert D._print_report(report, emit=lambda *a, **k: None) == 0   # first-stop is a clean exit


def test_continue_after_first_processes_all(tmp_path):
    jobs = [job("A", ["a2"]), job("B", ["b2"], index=2), job("C", ["c2"], index=3)]
    stub = make_stub(["committed", "committed", "committed"])
    report, _ = _run(jobs, tmp_path, commit_one=stub, continue_after_first=True)
    assert stub.calls == ["A", "B", "C"]
    assert report.stopped_after_first is False and report.counts["committed"] == 3


# ── 3. fail-stop (any mode != committed halts the whole run) ─────────────────────────
@pytest.mark.parametrize("bad_mode", ["blocked", "rolled_back", "rollback_failed", "shadow"])
def test_fail_stop_halts_run(tmp_path, bad_mode):
    jobs = [job("A", ["a2"]), job("B", ["b2"], index=2), job("C", ["c2"], index=3)]
    stub = make_stub(["committed", bad_mode])            # C must never be reached
    report, cp = _run(jobs, tmp_path, commit_one=stub, continue_after_first=True)
    assert stub.calls == ["A", "B"]                      # C NOT processed after the anomaly
    assert report.halted is not None and bad_mode in report.halted
    assert report.counts["committed"] == 1 and report.counts[bad_mode] == 1
    assert D._print_report(report, emit=lambda *a, **k: None) == 3   # non-zero exit
    # the checkpoint recorded both the commit and the halting outcome (audit trail)
    assert D.load_committed(cp) == {frozenset(["A", "a2"])}


def test_fail_stop_on_unexpected_exception(tmp_path):
    """A raw exception escaping commit_one becomes a clean halt (audit line + non-zero exit), never an
    uncaught traceback that skips the report and leaves no record."""
    calls = []

    def boom(j, reader, gateway, prov, *, library_id, now):
        calls.append(j.master)
        if j.master == "B":
            raise RuntimeError("simulated gateway 503 escaping merge_cluster")
        return D.LiveOutcome(D.CommitResult(mode="committed", verify_passed=True, trashed=list(j.secondaries),
                                            intent_prov_id=f"prov-{j.master}"), f"snap-{j.master}")

    jobs = [job("A", ["a2"]), job("B", ["b2"], index=2), job("C", ["c2"], index=3)]
    report, cp = _run(jobs, tmp_path, commit_one=boom, continue_after_first=True)
    assert calls == ["A", "B"]                       # C never reached
    assert report.halted is not None and "RuntimeError" in report.halted
    assert report.counts["error"] == 1
    assert D._print_report(report, emit=lambda *a, **k: None) == 3
    # audit: an "error" line exists for B; only A is committed
    import json as _json
    modes = [_json.loads(l)["mode"] for l in open(cp, encoding="utf-8") if l.strip()]
    assert modes == ["committed", "error"]


# ── 4. token refusal (refuses before any merge / network) ────────────────────────────
def test_token_refusal_main_refuses(monkeypatch):
    monkeypatch.delenv("ZOT_MERGE_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("ZOT_LIVE_APPLY_GATE", raising=False)
    assert D.main(["--live"]) == 1                       # refuses, no scan, no merge


def test_gate_helper(monkeypatch):
    monkeypatch.delenv("ZOT_MERGE_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("ZOT_LIVE_APPLY_GATE", raising=False)
    assert D._check_live_gates() is False
    monkeypatch.setenv("ZOT_MERGE_LIVE_ENABLED", ENABLE_TOKEN)
    assert D._check_live_gates() is False                # only one gate -> still refuse
    monkeypatch.setenv("ZOT_LIVE_APPLY_GATE", "I-UNDERSTAND")
    assert D._check_live_gates() is True


def test_shadow_refuses_if_live_token_set(monkeypatch):
    monkeypatch.setenv("ZOT_MERGE_LIVE_ENABLED", ENABLE_TOKEN)  # shadow must not run with the live token
    assert D.main([]) == 1


# ── 5. #52 / uncertain exclusion ─────────────────────────────────────────────────────
def test_uncertain_excluded_from_commit(tmp_path):
    u = job("256MZBTC", ["ASJD3SKQ"], is_dup="uncertain", index=52)
    jobs = [job("A", ["a2"]), u, job("C", ["c2"], index=53)]
    stub = make_stub(["committed", "committed"])
    report, _ = _run(jobs, tmp_path, commit_one=stub, continue_after_first=True)
    assert "256MZBTC" not in stub.calls                  # never committed
    assert stub.calls == ["A", "C"]
    assert report.counts["excluded"] == 1
    assert any(nc["members"] == ["256MZBTC", "ASJD3SKQ"] for nc in report.needs_confirm)


def test_uncertain_belt_by_member_set(tmp_path):
    """Belt-and-suspenders: even a MIS-labeled 'yes' record whose member set is the uncertain pair is
    excluded (a differently-shaped uncertain record cannot slip through)."""
    mislabeled = job("256MZBTC", ["ASJD3SKQ"], is_dup="yes", index=52)
    stub = make_stub([])                                 # must never be called
    report, _ = _run([mislabeled], tmp_path, commit_one=stub, continue_after_first=True)
    assert stub.calls == []
    assert report.counts["excluded"] == 1


def test_unmatched_excluded(tmp_path):
    unmatched = job("Z", ["z2"], is_dup="UNMATCHED")
    stub = make_stub([])
    report, _ = _run([unmatched], tmp_path, commit_one=stub, continue_after_first=True)
    assert stub.calls == [] and report.counts["excluded"] == 1


# ══ REAL CHAIN — the driver's live_commit_one against the engine, via a mutable FakeLibrary ══
def _i(key, version, itype, parent=None, **extra):
    data = {"key": key, "version": version, "itemType": itype, **extra}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": version, "data": data}


class FakeLibrary:
    """Mutable library serving as BOTH reader and gateway (copied contract from test_commit_merge.py)."""

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


def _raw():
    return {
        "M1": _i("M1", 100, "journalArticle", collections=["C1"], tags=[{"tag": "a", "type": 1}],
                 relations={}, title="Master"),
        "M2": _i("M2", 101, "journalArticle", collections=["C2"], tags=[{"tag": "b", "type": 1}],
                 relations={}, title="Dup"),
        "N1": _i("N1", 103, "note", parent="M2", note="n"),
    }


def test_live_commit_one_real_chain(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = FakeLibrary(_raw())
    prov = ProvenanceStore(tmp_path / "prov")
    daily_report(prov, ts=FRESH_TS)
    outcome = D.live_commit_one(job("M1", ["M2"]), lib, lib, prov, library_id=LIB, now=NOW)
    assert outcome.res.mode == "committed"
    assert lib.items["M2"]["data"]["deleted"] == 1        # TRASHED (deleted flag), not purged
    assert "M2" in lib.items                              # still present -> recoverable
    assert lib.items["N1"]["data"]["parentItem"] == "M1"  # child reparented to survivor
    assert outcome.snapshot_id


def test_run_real_chain_writes_checkpoint_and_first_stops(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    lib = FakeLibrary(_raw())
    prov = ProvenanceStore(tmp_path / "prov")
    cp = str(tmp_path / "live-checkpoint.jsonl")
    report = D.run([job("M1", ["M2"])], reader=lib, gateway=lib, prov=prov, mode="live", library_id=LIB,
                   checkpoint_path=cp, continue_after_first=False, clock=FIXED_CLOCK,
                   emit=lambda *a, **k: None)
    assert report.stopped_after_first and report.counts["committed"] == 1
    assert lib.items["M2"]["data"]["deleted"] == 1                 # real trash
    assert D.load_committed(cp) == {frozenset(["M1", "M2"])}       # checkpoint marks it committed
    # a resume finds nothing to do
    report2 = D.run([job("M1", ["M2"])], reader=lib, gateway=lib, prov=prov, mode="live", library_id=LIB,
                    checkpoint_path=cp, continue_after_first=True, clock=FIXED_CLOCK, emit=lambda *a, **k: None)
    assert report2.counts["skipped_resumed"] == 1 and report2.counts["committed"] == 0


def test_shadow_mode_no_writes_no_checkpoint(tmp_path):
    lib = FakeLibrary(_raw())
    prov = ProvenanceStore(tmp_path / "prov")
    cp = str(tmp_path / "live-checkpoint.jsonl")
    report = D.run([job("M1", ["M2"])], reader=lib, gateway=lib, prov=prov, mode="shadow", library_id=LIB,
                   checkpoint_path=cp, by_key={"M1": {"title": "Master"}}, emit=lambda *a, **k: None)
    assert len(report.preview) == 1
    assert lib.items["M2"]["data"].get("deleted") in (None, 0)     # NO trash in shadow
    assert not os.path.exists(cp)                                  # shadow writes no checkpoint
