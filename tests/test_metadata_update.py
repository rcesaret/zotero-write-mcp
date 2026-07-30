"""Routine Supervised v1.0 — the separate reviewed metadata update (PRD META-001/002/003)."""
import pytest

from zotero_write_mcp.gateway import ConcurrencyConflictError
from zotero_write_mcp.merge_live import ENABLE_ENV, ENABLE_TOKEN
from zotero_write_mcp.metadata import (
    METADATA_ALLOWLIST, apply_metadata_update, classify_fields, propose_metadata_update,
)
from zotero_write_mcp.provenance import ProvenanceStore

LIB = 11056739


class FakeItemStore:
    """Single-item reader + version-enforcing gateway."""

    def __init__(self, key="K1", version=50, **data):
        self.key = key
        self.version = version
        self.data = {"key": key, "version": version, "itemType": "journalArticle", **data}
        self.write_log = []

    def get_item(self, key):
        assert key == self.key
        return {"key": self.key, "version": self.version, "data": dict(self.data)}

    def update_item(self, library_id, item_key, data, version, *, library_type="user",
                    retry_on_412=True):
        if version != self.version:
            raise ConcurrencyConflictError(f"412: {version} != {self.version}")
        self.version += 1
        self.data.update(data)
        self.data["version"] = self.version
        self.write_log.append(dict(data))

    def external_edit(self, **data):
        self.version += 1
        self.data.update(data)
        self.data["version"] = self.version


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)


# ── META-002: explicit allowlist; identity/structure/state/unknown rejected ────


def test_classify_rejects_identity_structure_state_unknown():
    allowed, rejected = classify_fields({
        "title": "T", "publisher": "P",                      # allowed
        "extra": "Citation Key: hacked", "citationKey": "x", # identity
        "deleted": 1, "itemType": "note", "parentItem": "Z", # state/structure
        "collections": ["C"], "tags": [], "relations": {},   # structure
        "totallyMadeUpField": "v",                           # unknown
    })
    assert set(allowed) == {"title", "publisher"}
    assert set(rejected) == {"extra", "citationKey", "deleted", "itemType", "parentItem",
                             "collections", "tags", "relations", "totallyMadeUpField"}
    assert "identity" in rejected["extra"]
    assert "unknown" in rejected["totallyMadeUpField"]


def test_allowlist_contains_no_identity_structure_or_state_fields():
    forbidden = {"extra", "citationKey", "deleted", "itemType", "parentItem",
                 "collections", "tags", "relations", "version", "key", "dateAdded",
                 "dateModified", "mtime"}
    assert not (METADATA_ALLOWLIST & forbidden)


def test_propose_rejects_inadmissible_before_anything(tmp_path):
    store = FakeItemStore(title="Old")
    prov = ProvenanceStore(tmp_path / "p")
    out = propose_metadata_update(store, prov, "K1", {"deleted": 1})
    assert out["proposal_id"] is None and "rejected" in out
    assert store.write_log == []
    assert all(r["activity"] != "meta_update_proposed" for r in prov.all_records()), \
        "an inadmissible request must not even record a proposal"


# ── META-001: separate previewed operation with content-bound approval ─────────


def test_propose_and_apply_happy_path(tmp_path, live):
    store = FakeItemStore(title="Old title", publisher="")
    prov = ProvenanceStore(tmp_path / "p")
    p = propose_metadata_update(store, prov, "K1", {"title": "New title", "publisher": "UNM Press"})
    assert p["changes"]["title"] == {"from": "Old title", "to": "New title"}
    res = apply_metadata_update(p["proposal_id"], store, store, prov, library_id=LIB)
    assert res["state"] == "applied"
    assert store.data["title"] == "New title"
    assert store.data["publisher"] == "UNM Press"
    # Idempotency: a second apply performs no second mutation.
    n = len(store.write_log)
    res2 = apply_metadata_update(p["proposal_id"], store, store, prov, library_id=LIB)
    assert res2["state"] == "already_applied"
    assert len(store.write_log) == n


# ── META-003: a conflict blocks without overwriting the live value ─────────────


def test_version_change_between_preview_and_apply_blocks(tmp_path, live):
    store = FakeItemStore(title="Old title")
    prov = ProvenanceStore(tmp_path / "p")
    p = propose_metadata_update(store, prov, "K1", {"title": "Agent's new title"})
    store.external_edit(title="USER'S concurrent title")
    res = apply_metadata_update(p["proposal_id"], store, store, prov, library_id=LIB)
    assert res["state"] == "blocked_before_write"
    assert "NOT overwritten" in res["reason"]
    assert store.data["title"] == "USER'S concurrent title", "META-003: live value preserved exactly"
    assert store.write_log == []


def test_shadow_when_live_window_closed(tmp_path, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    store = FakeItemStore(title="Old")
    prov = ProvenanceStore(tmp_path / "p")
    p = propose_metadata_update(store, prov, "K1", {"title": "New"})
    res = apply_metadata_update(p["proposal_id"], store, store, prov, library_id=LIB)
    assert res["state"] == "shadow"
    assert store.data["title"] == "Old"
    assert store.write_log == []


def test_unknown_proposal_blocks(tmp_path, live):
    store = FakeItemStore(title="Old")
    prov = ProvenanceStore(tmp_path / "p")
    res = apply_metadata_update("META-nope", store, store, prov, library_id=LIB)
    assert res["state"] == "blocked_before_write"
    assert store.write_log == []


def test_unresolved_transactions_block_metadata_updates(tmp_path, live):
    store = FakeItemStore(title="Old")
    prov = ProvenanceStore(tmp_path / "p")
    p = propose_metadata_update(store, prov, "K1", {"title": "New"})
    prov.record(activity="merge_txn_unresolved", item_key="M9", params={"transaction_id": "T9"})
    res = apply_metadata_update(p["proposal_id"], store, store, prov, library_id=LIB)
    assert res["state"] == "blocked_before_write" and "unresolved" in res["reason"]
    assert store.data["title"] == "Old"


def test_damaged_log_blocks_metadata_updates(tmp_path, live):
    store = FakeItemStore(title="Old")
    prov = ProvenanceStore(tmp_path / "p")
    p = propose_metadata_update(store, prov, "K1", {"title": "New"})
    with open(prov.prov_path, "ab") as f:
        f.write(b"damaged\n")
    res = apply_metadata_update(p["proposal_id"], store, store, prov, library_id=LIB)
    assert res["state"] == "blocked_before_write" and "integrity" in res["reason"]
    assert store.data["title"] == "Old"
