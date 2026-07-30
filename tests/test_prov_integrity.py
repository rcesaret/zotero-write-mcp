"""Routine Supervised v1.0 — PROV log integrity (PRD LOG-001/LOG-002/LOG-003).

Covers the reimplemented intent of engine commit ef9bf67 (torn line must not hide the tail;
newline-defensive append) plus the v1 fail-closed integrity scan: malformed state becomes an
explicit blocking status, resolved only by an exact, owner-recorded acceptance — never by
silently skipping evidence, and never by rewriting the append-only log.
"""
import json

import pytest

from zotero_write_mcp.provenance import ProvenanceStore


def _store(tmp_path) -> ProvenanceStore:
    return ProvenanceStore(tmp_path / "prov")


def _append_raw(store: ProvenanceStore, raw: bytes) -> None:
    with open(store.prov_path, "ab") as f:
        f.write(raw)


# ── LOG-002: a torn line must not hide the tail ─────────────────────────────────


def test_malformed_midfile_line_does_not_hide_later_records(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    _append_raw(store, b'{"torn": \n')          # malformed but newline-terminated
    store.record(activity="a2", item_key="K2")
    acts = [r["activity"] for r in store.iter_records()]
    assert acts == ["a1", "a2"], "the record AFTER the torn line must remain visible"


def test_torn_tail_then_append_does_not_concatenate(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    _append_raw(store, b'{"prov_id": "torn-no-newline"')     # crash mid-append: no newline
    rec2 = store.record(activity="a2", item_key="K2")
    acts = [r["activity"] for r in store.iter_records()]
    assert acts == ["a1", "a2"], "newline-defensive append must keep the next record parseable"
    # And the torn fragment is still individually visible to the scan (not merged into rec2).
    report = store.scan_integrity()
    assert len(report["bad_lines"]) == 1
    assert report["bad_lines"][0]["preview"].startswith('{"prov_id": "torn-no-newline"')
    assert rec2["prov_id"] in {r.get("prov_id") for r in store.iter_records()}


def test_blank_lines_are_not_damage(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    _append_raw(store, b"\n\n")
    store.record(activity="a2", item_key="K2")
    report = store.scan_integrity()
    assert report["status"] == "ok"
    assert report["valid_records"] == 2


# ── LOG-001: malformed state blocks; nothing is silently omitted ────────────────


def test_scan_integrity_ok_on_clean_log(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    store.record(activity="a2", item_key="K2")
    report = store.scan_integrity()
    assert report["status"] == "ok"
    assert report["total_lines"] == 2
    assert report["valid_records"] == 2
    assert report["bad_lines"] == []


def test_scan_integrity_blocked_on_malformed_line(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    _append_raw(store, b"not json at all\n")
    store.record(activity="a2", item_key="K2")
    report = store.scan_integrity()
    assert report["status"] == "blocked"
    (bad,) = report["bad_lines"]
    assert bad["line_no"] == 2
    assert bad["at_eof"] is False
    assert bad["reason"] == "malformed"
    assert report["unaccepted"] == report["bad_lines"]


def test_scan_integrity_marks_torn_tail_at_eof(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    _append_raw(store, b'{"half":')             # torn crash tail, no newline
    report = store.scan_integrity()
    assert report["status"] == "blocked"
    (bad,) = report["bad_lines"]
    assert bad["at_eof"] is True


def test_scan_integrity_empty_log_is_ok(tmp_path):
    report = _store(tmp_path).scan_integrity()
    assert report["status"] == "ok"
    assert report["total_lines"] == 0


def test_non_dict_json_line_is_damage(tmp_path):
    store = _store(tmp_path)
    _append_raw(store, b'[1, 2, 3]\n')          # valid JSON, but not a record object
    report = store.scan_integrity()
    assert report["status"] == "blocked"


# ── Owner acceptance: explicit, exact, non-destructive ──────────────────────────


def test_acceptance_unblocks_exactly_the_named_line(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    _append_raw(store, b"damaged-line-one\n")
    report = store.scan_integrity()
    (bad,) = report["bad_lines"]

    store.accept_log_damage(bad["line_no"], bad["line_sha256"], note="inspected 2026-07-30; crash tail")
    after = store.scan_integrity()
    assert after["status"] == "accepted_damage"
    assert after["unaccepted"] == []
    # The damage stays permanently visible — acceptance is not erasure.
    assert len(after["accepted"]) == 1
    assert after["accepted"][0]["line_sha256"] == bad["line_sha256"]


def test_acceptance_of_wrong_sha_does_not_unblock(tmp_path):
    store = _store(tmp_path)
    _append_raw(store, b"damaged-line-one\n")
    (bad,) = store.scan_integrity()["bad_lines"]
    store.accept_log_damage(bad["line_no"], "0" * 64, note="wrong sha on purpose")
    assert store.scan_integrity()["status"] == "blocked", \
        "an acceptance that does not match the exact damaged bytes must not unblock"


def test_new_damage_after_acceptance_blocks_again(tmp_path):
    store = _store(tmp_path)
    _append_raw(store, b"first-damage\n")
    (bad,) = store.scan_integrity()["bad_lines"]
    store.accept_log_damage(bad["line_no"], bad["line_sha256"], note="ok")
    assert store.scan_integrity()["status"] == "accepted_damage"
    _append_raw(store, b"second-damage\n")
    assert store.scan_integrity()["status"] == "blocked"


def test_acceptance_requires_note(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.accept_log_damage(1, "ab" * 32, note="   ")


def test_acceptance_never_rewrites_the_log(tmp_path):
    store = _store(tmp_path)
    store.record(activity="a1", item_key="K1")
    _append_raw(store, b"damaged\n")
    before_bytes = store.prov_path.read_bytes()
    (bad,) = store.scan_integrity()["bad_lines"]
    store.accept_log_damage(bad["line_no"], bad["line_sha256"], note="ok")
    after_bytes = store.prov_path.read_bytes()
    assert after_bytes.startswith(before_bytes), "append-only: acceptance must not rewrite history"


# ── Bounds ──────────────────────────────────────────────────────────────────────


def test_oversized_line_is_damage_without_parse(tmp_path):
    store = _store(tmp_path)
    big = b'{"pad": "' + b"x" * (ProvenanceStore.MAX_LINE_BYTES + 10) + b'"}\n'
    _append_raw(store, big)
    store.record(activity="after", item_key="K2")
    report = store.scan_integrity()
    assert report["status"] == "blocked"
    assert report["bad_lines"][0]["reason"] == "oversized"
    # iter_records still sees the record after the oversized line.
    assert [r["activity"] for r in store.iter_records()] == ["after"]


def test_bad_line_detail_is_bounded(tmp_path):
    store = _store(tmp_path)
    for _ in range(ProvenanceStore.MAX_BAD_LINE_DETAIL + 7):
        _append_raw(store, b"junk\n")
    report = store.scan_integrity()
    assert report["status"] == "blocked"
    assert len(report["bad_lines"]) == ProvenanceStore.MAX_BAD_LINE_DETAIL
