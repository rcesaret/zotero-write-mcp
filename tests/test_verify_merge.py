"""Unit tests for verify_merge — golden projection passes 11/11; one isolated failure per invariant.

(The INDEPENDENT adversarial injection suite is built separately by a red-team agent at the Phase-1
gate; this file is the author's own per-check pass/fail coverage required by build-discipline.)"""
import copy

import pytest

from zotero_write_mcp.merge import (
    snapshot_cluster, compute_merge_projection, verify_merge,
)
from zotero_write_mcp.provenance import ProvenanceStore


class FakeReader:
    def __init__(self, items, children, annotations, citekeys):
        self._items, self._children = items, children
        self._annotations, self._citekeys = annotations, citekeys

    def get_item(self, key):
        return self._items[key]

    def get_children(self, key):
        return self._children.get(key, [])

    def get_annotations(self, attachment_key):
        return self._annotations.get(attachment_key, [])

    def get_citekey(self, key):
        return self._citekeys.get(key)


def _item(key, version, item_type="journalArticle", **data):
    d = {"key": key, "version": version, "itemType": item_type, **data}
    return {"key": key, "version": version, "data": d}


def build_snapshot(tmp_path):
    items = {
        "M1": _item("M1", 100, title="Basin of Mexico", collections=["C1"],
                    tags=[{"tag": "aztec", "type": 1}], relations={}),
        "M2": _item("M2", 101, title="Basin of Mexico dup", collections=["C2"],
                    tags=[{"tag": "maya", "type": 1}], relations={"dc:relation": ["http://x/items/Z9"]}),
    }
    children = {
        "M1": [
            _item("N1", 110, item_type="note", parentItem="M1", note="n"),
            _item("A1", 111, item_type="attachment", parentItem="M1",
                  md5="abc", filename="p.pdf", contentType="application/pdf"),
        ],
        "M2": [
            _item("A2", 112, item_type="attachment", parentItem="M2",
                  md5="def", filename="d.pdf", contentType="application/pdf"),
        ],
    }
    annotations = {"A1": [_item("AN1", 120, item_type="annotation", parentItem="A1")]}
    citekeys = {"M1": "ck-master", "M2": "ck-dup"}
    reader = FakeReader(items, children, annotations, citekeys)
    return snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path))


@pytest.fixture
def snap(tmp_path):
    return build_snapshot(tmp_path)


def failed(report):
    return {c.name for c in report.failed}


# ── Golden path ────────────────────────────────────────────────────────────────

def test_golden_projection_passes_all_11(snap):
    rep = verify_merge(snap, compute_merge_projection(snap))
    assert rep.passed, rep.to_dict()
    # union collections, union tags, dc:replaces->M2, children reparented, all intact
    assert len(rep.checks) >= 11


def _broken(snap, mutate):
    proj = copy.deepcopy(compute_merge_projection(snap))
    mutate(proj)
    return verify_merge(snap, proj)


# ── One isolated failure injection per invariant ───────────────────────────────

def test_1_item_type(snap):
    def m(p): p.items["M2"].item_type = "book"
    r = _broken(snap, m)
    assert not r.passed and "item-type-equality" in failed(r)


def test_2_version_drift(snap):
    def m(p): p.items["M2"].version = 999          # secondary edited mid-merge
    r = _broken(snap, m)
    assert not r.passed and "version-drift" in failed(r)


def test_3_master_scalar_overwrite(snap):
    def m(p): p.items["M1"].fields["title"] = "CLOBBERED"   # smart_fill silently overwrote survivor
    r = _broken(snap, m)
    assert not r.passed and "master-scalar-preservation" in failed(r)


def test_4_collections_not_equal(snap):
    def m(p): p.items["M1"].collections = ["C1"]            # dropped C2 from the union
    r = _broken(snap, m)
    assert not r.passed and "collections-equality" in failed(r)


def test_5_tags_dropped(snap):
    def m(p): p.items["M1"].tags = [("aztec", 1)]           # lost the maya tag
    r = _broken(snap, m)
    assert not r.passed and "tags-tuple-superset" in failed(r)


def test_5_tag_type_flip(snap):
    def m(p): p.items["M1"].tags = [("aztec", 0), ("maya", 1)]  # aztec flipped 1->0
    r = _broken(snap, m)
    assert not r.passed and "tags-tuple-superset" in failed(r)


def test_6_dc_replaces_missing(snap):
    def m(p): p.items["M1"].relations["dc:replaces"] = []   # forgot dc:replaces->secondary
    r = _broken(snap, m)
    assert not r.passed and "relations-superset" in failed(r)


def test_6_relation_dropped(snap):
    def m(p): p.items["M1"].relations.pop("dc:relation", None)  # lost a pre-existing relation
    r = _broken(snap, m)
    assert not r.passed and "relations-superset" in failed(r)


def test_7_child_misparented(snap):
    def m(p):
        for n in p.notes:
            n.parent_key = "OTHER"                          # note not reparented to master
    r = _broken(snap, m)
    assert not r.passed and "child-completeness" in failed(r)


def test_8_note_count(snap):
    def m(p): p.notes = []                                  # a note vanished
    r = _broken(snap, m)
    assert not r.passed and "note-count-parity" in failed(r)


def test_8_attachment_count(snap):
    def m(p): p.attachments = p.attachments[:1]             # an attachment vanished
    r = _broken(snap, m)
    assert not r.passed and "attachment-count-parity" in failed(r)


def test_9_annotation_parity(snap):
    def m(p):
        for a in p.attachments:
            a.annotations = []                              # lost an annotation grandchild
    r = _broken(snap, m)
    assert not r.passed and "annotation-parity" in failed(r)


def test_10_storage_integrity(snap):
    def m(p):
        for a in p.attachments:
            if a.key == "A1":
                a.md5 = "TAMPERED"                          # storage association changed
    r = _broken(snap, m)
    assert not r.passed and "attachment-storage-integrity" in failed(r)


def test_11_citekey_changed(snap):
    def m(p): p.items["M1"].citekey = "ck-different"        # citekey not preserved
    r = _broken(snap, m)
    assert not r.passed and "citekey-preservation" in failed(r)


def test_master_absent_fails_closed(snap):
    proj = copy.deepcopy(compute_merge_projection(snap))
    del proj.items["M1"]
    r = verify_merge(snap, proj)
    assert not r.passed


def test_smart_fill_allows_empty_field_fill(tmp_path):
    """smart_fill may fill a snapshot-EMPTY master field from a secondary; #3 still passes."""
    items = {
        "M1": _item("M1", 1, title="t", abstractNote="", collections=[], tags=[], relations={}),
        "M2": _item("M2", 2, title="t2", abstractNote="filled in", collections=[], tags=[], relations={}),
    }
    reader = FakeReader(items, {"M1": [], "M2": []}, {}, {"M1": "ck", "M2": "ck2"})
    snap = snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path))
    proj = compute_merge_projection(snap, smart_fill=True)
    assert proj.items["M1"].fields["abstractNote"] == "filled in"
    rep = verify_merge(snap, proj, smart_fill=True)
    assert rep.passed, rep.to_dict()
