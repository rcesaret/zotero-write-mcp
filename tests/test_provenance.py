"""Tests for the provenance + content-addressed blob store (Phase 0, P0-prov-store).

Pure-offline: no Zotero, no network. Verifies the append-only audit log, the content-addressed
blob store, canonical hashing, durability/recovery, and the query path that backs query_provenance.
"""
import json

import pytest

from zotero_write_mcp.provenance import (
    ProvenanceStore,
    canonical_json,
    json_sha256,
    sha256_hex,
)


# ── Canonical hashing ─────────────────────────────────────────────────────────

def test_canonical_json_is_key_order_independent():
    a = {"title": "X", "year": 1979, "creators": [{"l": "Sanders"}]}
    b = {"creators": [{"l": "Sanders"}], "year": 1979, "title": "X"}
    assert canonical_json(a) == canonical_json(b)
    assert json_sha256(a) == json_sha256(b)


def test_json_sha256_none_is_none():
    assert json_sha256(None) is None


def test_json_sha256_distinguishes_different_objects():
    assert json_sha256({"a": 1}) != json_sha256({"a": 2})


def test_sha256_hex_matches_hashlib():
    import hashlib
    assert sha256_hex(b"hello") == hashlib.sha256(b"hello").hexdigest()


# ── Blob store ────────────────────────────────────────────────────────────────

def test_put_blob_is_content_addressed_and_idempotent(tmp_path):
    store = ProvenanceStore(tmp_path)
    data = b"some attachment bytes"
    d1 = store.put_blob(data)
    d2 = store.put_blob(data)  # idempotent
    assert d1 == d2 == sha256_hex(data)
    assert store.has_blob(d1)
    assert store.get_blob(d1) == data
    # stored exactly once at blobs/<h[:2]>/<h>
    blob_file = tmp_path / "blobs" / d1[:2] / d1
    assert blob_file.is_file()
    # no leftover temp files
    assert not list((tmp_path / "blobs" / d1[:2]).glob("*.tmp-*"))


def test_put_json_blob_roundtrips(tmp_path):
    store = ProvenanceStore(tmp_path)
    obj = {"itemType": "journalArticle", "title": "Basin of Mexico", "tags": [{"tag": "x"}]}
    digest = store.put_json_blob(obj)
    assert digest == json_sha256(obj)
    assert store.get_json_blob(digest) == obj


def test_put_json_blob_none_returns_none(tmp_path):
    assert ProvenanceStore(tmp_path).put_json_blob(None) is None


# ── PROV record ───────────────────────────────────────────────────────────────

def test_record_captures_hashes_and_reversible_blobs(tmp_path):
    store = ProvenanceStore(tmp_path)
    before = {"key": "ABCD1234", "version": 10, "title": "Old"}
    after = {"key": "ABCD1234", "version": 11, "title": "New"}
    rec = store.record(
        activity="update_item_fields",
        item_key="ABCD1234",
        before=before,
        after=after,
        agent="zot-maintain",
        tool_version="0.2.0",
        params={"fields": {"title": "New"}},
        snapshot_id="snap-001",
    )
    ent = rec["entity"]
    assert ent["item_key"] == "ABCD1234"
    assert ent["before_sha256"] == json_sha256(before)
    assert ent["after_sha256"] == json_sha256(after)
    assert rec["was_derived_from"] == "snap-001"
    assert rec["activity"] == "update_item_fields"
    assert rec["agent"] == "zot-maintain"
    # reversibility: the before/after images are recoverable byte-for-byte
    assert store.get_json_blob(ent["before_blob"]) == before
    assert store.get_json_blob(ent["after_blob"]) == after


def test_record_requires_activity(tmp_path):
    with pytest.raises(ValueError):
        ProvenanceStore(tmp_path).record(activity="", item_key="X")


def test_record_store_blobs_false_keeps_hashes_only(tmp_path):
    store = ProvenanceStore(tmp_path)
    rec = store.record(
        activity="delete_item", item_key="X", before={"a": 1}, store_blobs=False
    )
    assert rec["entity"]["before_sha256"] == json_sha256({"a": 1})
    assert rec["entity"]["before_blob"] is None


# ── Append-only log + durability ──────────────────────────────────────────────

def test_records_are_appended_in_order(tmp_path):
    store = ProvenanceStore(tmp_path)
    for i in range(5):
        store.record(activity="create_item", item_key=f"K{i}", after={"n": i})
    recs = store.all_records()
    assert [r["entity"]["item_key"] for r in recs] == ["K0", "K1", "K2", "K3", "K4"]
    # one physical line per record
    assert (tmp_path / "prov.jsonl").read_text(encoding="utf-8").strip().count("\n") == 4
    assert store.count() == 5


def test_persists_across_reopen(tmp_path):
    ProvenanceStore(tmp_path).record(activity="create_item", item_key="K", after={"x": 1})
    reopened = ProvenanceStore(tmp_path)  # fresh instance, same root
    assert reopened.count() == 1
    assert reopened.all_records()[0]["entity"]["item_key"] == "K"


def test_query_filters_by_item_key(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.record(activity="create_item", item_key="A", after={})
    store.record(activity="update_item_fields", item_key="B", after={})
    store.record(activity="add_tags", item_key="A", after={})
    a_hist = store.query("A")
    assert len(a_hist) == 2
    assert {r["activity"] for r in a_hist} == {"create_item", "add_tags"}


def test_partial_trailing_line_is_skipped_not_fatal(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.record(activity="create_item", item_key="A", after={})
    store.record(activity="create_item", item_key="B", after={})
    # simulate a crash mid-append: a truncated, invalid trailing line
    with open(tmp_path / "prov.jsonl", "a", encoding="utf-8") as f:
        f.write('{"prov_id": "partial", "entity": {"item_key": "C"')  # no newline, invalid JSON
    recs = store.all_records()
    assert [r["entity"]["item_key"] for r in recs] == ["A", "B"]  # earlier records intact


def test_prov_record_is_valid_json_line(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.record(activity="create_item", item_key="A", after={"t": "x"})
    line = (tmp_path / "prov.jsonl").read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["activity"] == "create_item"
    assert "ts" in parsed and "prov_id" in parsed
