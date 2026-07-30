"""Routine Supervised v1.0 — central admissibility covers EVERY producer (PRD MRG-003; §5.3 #3/#4).

The F2 mechanism had two producer routes: field_sources (closed by _UNRECONCILABLE_FIELDS in
_enriched_fields) and smart_fill, which filtered only _PROTECTED_FIELDS — so a trashed secondary's
`deleted: 1` (empty on a live master) qualified as a fillable scalar. These regressions prove
structure/state cannot enter through ANY scalar producer: the projection, verify's expectation,
and the live PATCH body.

Note: smart_fill is entirely ABSENT from the v1 production transaction surface; these tests defend
the legacy engine functions (direct driver / tests) as defense in depth.
"""
from zotero_write_mcp.merge import (
    _UNRECONCILABLE_FIELDS, build_cluster, compute_merge_projection, snapshot_cluster, verify_merge,
)
from zotero_write_mcp.merge_live import merge_cluster
from zotero_write_mcp.provenance import ProvenanceStore

LIB = 11056739


def _i(key, version, itype, parent=None, **extra):
    data = {"key": key, "version": version, "itemType": itype, **extra}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "version": version, "data": data}


class FakeLibrary:
    def __init__(self, items):
        self.items = items
        self.lib_ver = max(it["version"] for it in items.values())
        self.write_log = []

    def get_item(self, key):
        return self.items[key]

    def get_children(self, key):
        return [it for it in self.items.values()
                if it["data"].get("parentItem") == key
                and it["data"].get("itemType") in ("note", "attachment")]

    def get_annotations(self, attachment_key):
        return []

    def get_citekey(self, key):
        return None

    def update_item(self, library_id, item_key, data, version, *, library_type="user",
                    retry_on_412=True):
        it = self.items[item_key]
        self.lib_ver += 1
        it["data"].update(data)
        it["version"] = self.lib_ver
        it["data"]["version"] = self.lib_ver
        self.write_log.append({"key": item_key, "data": dict(data)})


def make_raw_with_trashed_secondary():
    """M2 is ALREADY in the trash (deleted=1) — the standing rollback substrate shape: the library
    keeps 153 trashed S2 secondaries, so a trashed member to smart-fill from is always available."""
    return {
        "M1": _i("M1", 100, "journalArticle", collections=[], tags=[], relations={},
                 title="Master"),                          # master: no `deleted` key (alive)
        "M2": _i("M2", 101, "journalArticle", collections=[], tags=[], relations={},
                 title="Dup", deleted=1, publisher="Filler Press"),
    }


def test_projection_smart_fill_cannot_produce_deleted(tmp_path):
    lib = FakeLibrary(make_raw_with_trashed_secondary())
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=ProvenanceStore(tmp_path / "p"))
    proj = compute_merge_projection(snap, smart_fill=True)
    assert "deleted" not in proj.items["M1"].fields, \
        "smart_fill projection must never transfer the deleted flag (§5.3 #3)"
    # Ordinary bibliographic fill still works — the guard is precise, not a blanket disable.
    assert proj.items["M1"].fields.get("publisher") == "Filler Press"


def test_verify_expectation_smart_fill_cannot_expect_deleted(tmp_path):
    """Verify's own smart_fill expectation must not EXPECT `deleted` either — otherwise a corrupted
    write of deleted=1 would be gate-approved (the V11-02 tautology cashed in)."""
    lib = FakeLibrary(make_raw_with_trashed_secondary())
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=ProvenanceStore(tmp_path / "p"))
    # Simulate a rogue write: the master got deleted=1 anyway; observed state carries it.
    lib.items["M1"]["data"]["deleted"] = 1
    observed = build_cluster(lib, "M1", ["M2"])
    report = verify_merge(snap, observed, smart_fill=True)
    failed = {c.name for c in report.failed}
    assert "master-scalar-preservation" in failed, \
        "a deleted flag on the survivor must FAIL check #3 even under smart_fill"


def test_live_patch_body_smart_fill_cannot_carry_state(tmp_path):
    lib = FakeLibrary(make_raw_with_trashed_secondary())
    snap = snapshot_cluster(lib, "M1", ["M2"], prov=ProvenanceStore(tmp_path / "p"))
    plan = merge_cluster(snap, lib, lib, library_id=LIB, smart_fill=True)
    assert not plan.drifted
    master_patch = next(w for w in lib.write_log if w["key"] == "M1")
    for fld in ("deleted", "itemType", "parentItem"):
        assert fld not in master_patch["data"], f"{fld} must never appear in the master PATCH body"
    assert master_patch["data"].get("publisher") == "Filler Press"
    assert lib.items["M1"]["data"].get("deleted") in (None, 0), "survivor stays alive"


def test_unreconcilable_set_covers_structure_and_state():
    for fld in ("deleted", "itemType", "parentItem", "collections", "tags", "relations",
                "version", "key", "dateAdded", "dateModified", "mtime"):
        assert fld in _UNRECONCILABLE_FIELDS, fld
