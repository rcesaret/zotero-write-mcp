"""The survivor must never end up in the trash with the merge reporting success.

S5b Stage-E #2, finding F2 — the pass's one genuine BLOCKER, and it SURVIVES the
projection-vs-live-reread filter (unlike V11-01, which was withdrawn: `_terminal_verify` reads a real
`build_cluster` re-read, so nothing here depends on a self-consistent projection).

WHAT WAS BROKEN. `_terminal_verify` (C-5, the last gate before `commit_merge` reports success) called
`_is_trashed` on every CHILD and on every SECONDARY — which must BE trashed — but for the master it
asked only `if fm is None`. A trashed survivor is not None; it is present carrying `deleted:1`. So:

    mode='committed'  verify_passed=True  trashed=['M2']  SURVIVOR deleted=1  PROV pass=True

The symmetric `verify_merge` catches the same state (check #3: "added=['deleted']"), which is exactly
the asymmetry — C-5 is the only gate that runs AFTER the trash, where verify_merge cannot.

TWO TRIGGERS, and the first needs no race:
  1. `field_sources={"deleted": <a member already in the trash>}`. `field_sources` is free-form,
     `_enriched_fields` skipped only `citationKey`, and `deleted` was absent from `_NON_SCALAR_FIELDS`,
     so it was reconciled as an ordinary scalar onto the survivor. Because a `field_sources` field is an
     APPROVED override, check #3 EXPECTED `deleted` and the 11-check gate passed — V11-02's tautology
     cashed in. The library keeps 153 trashed secondaries as rollback substrate, so a member to point at
     is always available.
  2. The survivor is trashed inside the post-verify trash window (owner action in the Zotero UI, a sync
     from another device, or a mis-targeted DELETE).

Both halves are fixed: the missing guard in `_terminal_verify`, and the root cause in `_enriched_fields`.
"""
from __future__ import annotations

from zotero_write_mcp.merge import (
    _UNRECONCILABLE_FIELDS,
    _enriched_fields,
    _is_trashed,
    build_cluster,
    compute_merge_projection,
    snapshot_cluster,
    verify_merge,
)
from zotero_write_mcp.merge_live import _terminal_verify
from zotero_write_mcp.provenance import ProvenanceStore

BASE = "http://zotero.org/users/0/items"


def _item(key, version, item_type="journalArticle", **data):
    d = {"key": key, "version": version, "itemType": item_type, **data}
    return {"key": key, "version": version, "data": d}


class Reader:
    """Production-shaped: includes trashed items (the documented commit-time reader contract) and
    derives the citekey from the item's current data."""

    def __init__(self, items):
        self._items = items

    def get_item(self, key):
        return self._items[key]

    def get_children(self, key):
        return []

    def get_annotations(self, attachment_key):
        return []

    def get_citekey(self, key):
        from zotero_write_mcp.merge import _citekey_from_extra
        data = self.get_item(key).get("data", {}) or {}
        return _citekey_from_extra(data.get("extra")) or data.get("citationKey") or None


def _cluster(tmp_path, dup_trashed=False):
    dup = dict(title="Basin dup", collections=["C1"], tags=[], relations={})
    if dup_trashed:
        dup["deleted"] = 1
    items = {
        "M1": _item("M1", 100, title="Basin of Mexico", collections=["C1"], tags=[], relations={}),
        "M2": _item("M2", 101, **dup),
    }
    reader = Reader(items)
    return items, snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path))


def _final(items, snap, *, master_deleted=False, secondaries_trashed=True, field_sources=None):
    """The post-commit live state, built the way the real merge leaves it: the survivor carries the
    merge's own PATCH (unioned collections/tags + dc:replaces + scalar overrides) taken from the
    projection, the secondaries are trashed, and the master is optionally trashed. Applying the PATCH
    matters — a fixture that skips it fails `master-relations` as an artifact and tells you nothing
    about the guard under test."""
    proj_m = compute_merge_projection(snap, library_base=BASE, field_sources=field_sources).items["M1"]
    post = {k: {"key": v["key"], "version": v["version"], "data": dict(v["data"])}
            for k, v in items.items()}
    post["M1"]["data"].update(proj_m.fields)
    post["M1"]["data"]["collections"] = list(proj_m.collections)
    post["M1"]["data"]["tags"] = [{"tag": t[0], "type": t[1]} for t in proj_m.tags]
    post["M1"]["data"]["relations"] = dict(proj_m.relations)
    if secondaries_trashed:
        post["M2"]["data"]["deleted"] = 1
    if master_deleted:
        post["M1"]["data"]["deleted"] = 1
    return build_cluster(Reader(post), "M1", ["M2"])


# ── the C-5 guard ────────────────────────────────────────────────────────────────────────────────

def test_terminal_verify_FAILS_when_the_survivor_is_trashed(tmp_path):
    """THE DECISIVE CASE. Before the fix this returned passed=True with failed=[]."""
    items, snap = _cluster(tmp_path)
    final = _final(items, snap, master_deleted=True)
    assert _is_trashed(final.items["M1"]) is True, "fixture did not actually trash the survivor"

    report = _terminal_verify(snap, final, ["M2"], library_base=BASE)
    assert report.passed is False, "C-5 accepted a merge that left the SURVIVOR in the trash"
    assert "master-trashed" in report.failed


def test_terminal_verify_still_PASSES_a_clean_merge(tmp_path):
    """No regression: the guard must not reject the normal case."""
    items, snap = _cluster(tmp_path)
    report = _terminal_verify(snap, _final(items, snap), ["M2"], library_base=BASE)
    assert report.passed is True, f"clean merge rejected: {report.failed}"


def test_terminal_verify_still_requires_the_secondaries_to_BE_trashed(tmp_path):
    """The guard is direction-sensitive: secondaries must be trashed, the master must not."""
    items, snap = _cluster(tmp_path)
    report = _terminal_verify(snap, _final(items, snap, secondaries_trashed=False), ["M2"],
                              library_base=BASE)
    assert report.passed is False
    assert any(f.startswith("secondary-not-trashed") for f in report.failed)


def test_a_trashed_master_is_distinguished_from_an_absent_one(tmp_path):
    """Two different failures with two different remedies — do not collapse them."""
    items, snap = _cluster(tmp_path)
    trashed = _terminal_verify(snap, _final(items, snap, master_deleted=True), ["M2"], library_base=BASE)
    assert "master-trashed" in trashed.failed and "master-absent" not in trashed.failed


# ── the root cause: field_sources may not reconcile structure or state ───────────────────────────

def test_field_sources_cannot_reconcile_the_deleted_flag(tmp_path):
    """Trigger 1, deterministic and race-free. `deleted` must never reach the survivor's PATCH."""
    _, snap = _cluster(tmp_path, dup_trashed=True)
    assert snap.items["M2"].fields.get("deleted") == 1, "fixture: the dup must be trashed at snapshot"
    assert "deleted" not in _enriched_fields(snap, {"deleted": "M2"})


def test_the_deleted_flag_never_reaches_the_projected_survivor(tmp_path):
    """End of the same trigger: the projection the PATCH is built from must carry no `deleted`."""
    _, snap = _cluster(tmp_path, dup_trashed=True)
    proj = compute_merge_projection(snap, field_sources={"deleted": "M2"})
    assert not proj.items["M1"].fields.get("deleted")
    assert _is_trashed(proj.items["M1"]) is False


def test_the_11_check_gate_no_longer_expects_a_deleted_survivor(tmp_path):
    """The tautology that let trigger 1 through: `deleted` under field_sources was an APPROVED override,
    so check #3 EXPECTED it. With the field refused, a trashed survivor is a deviant ADDITION again and
    #3's symmetric upper bound rejects it."""
    items, snap = _cluster(tmp_path, dup_trashed=True)
    fs = {"deleted": "M2"}
    report = verify_merge(snap, _final(items, snap, master_deleted=True, field_sources=fs), field_sources=fs)
    assert report.passed is False
    assert "master-scalar-preservation" in {c.name for c in report.failed}


def test_structural_fields_are_all_refused(tmp_path):
    """One guard, not a special case for `deleted`: version/key/itemType/collections and friends are
    structure or state, never reconcilable metadata."""
    _, snap = _cluster(tmp_path, dup_trashed=True)
    for fld in ("deleted", "itemType", "key", "version", "dateAdded", "dateModified",
                "collections", "tags", "relations", "parentItem"):
        assert fld in _UNRECONCILABLE_FIELDS, fld
        assert fld not in _enriched_fields(snap, {fld: "M2"}), f"{fld} was reconciled"


def test_ordinary_metadata_is_still_reconcilable(tmp_path):
    """The guard must not break Phase-B enrichment, which is the whole point of field_sources."""
    _, snap = _cluster(tmp_path, dup_trashed=True)
    snap.items["M2"].fields["title"] = "A Better Title"
    assert _enriched_fields(snap, {"title": "M2"}) == {"title": "A Better Title"}
