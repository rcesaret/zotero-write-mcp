"""Unit tests for snapshot_cluster (P1-snapshot) — fake reader + temp PROV store, no network."""
import pytest

from zotero_write_mcp.merge import snapshot_cluster, ClusterSnapshot
from zotero_write_mcp.provenance import ProvenanceStore


class FakeReader:
    """Canned, version-accurate item/children/annotation/citekey data."""

    def __init__(self, items, children, annotations, citekeys):
        self._items = items
        self._children = children
        self._annotations = annotations
        self._citekeys = citekeys

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


@pytest.fixture
def cluster():
    """Master M1 (note N1 + attachment A1 with annotation AN1) + secondary M2 (attachment A2)."""
    items = {
        "M1": _item("M1", 100, title="Basin of Mexico", collections=["COL1"],
                    tags=[{"tag": "aztec", "type": 1}], relations={}),
        "M2": _item("M2", 101, title="Basin of Mexico (dup)", collections=["COL2"],
                    tags=[{"tag": "aztec"}], relations={"dc:relation": ["x"]}),
    }
    children = {
        "M1": [
            _item("N1", 102, item_type="note", parentItem="M1", note="a note"),
            _item("A1", 103, item_type="attachment", parentItem="M1",
                  md5="abc123", filename="paper.pdf", contentType="application/pdf"),
        ],
        "M2": [
            _item("A2", 104, item_type="attachment", parentItem="M2",
                  md5="def456", filename="dup.pdf", contentType="application/pdf"),
        ],
    }
    annotations = {"A1": [_item("AN1", 105, item_type="annotation", parentItem="A1",
                                annotationType="highlight")]}
    citekeys = {"M1": "sandersBasinMexico1979", "M2": "sandersBasinMexico1979a"}
    return FakeReader(items, children, annotations, citekeys)


def test_snapshot_captures_cluster(cluster, tmp_path):
    prov = ProvenanceStore(tmp_path)
    snap = snapshot_cluster(cluster, "M1", ["M2"], prov=prov)

    assert isinstance(snap, ClusterSnapshot)
    assert snap.master_key == "M1" and snap.secondary_keys == ["M2"]
    assert set(snap.items) == {"M1", "M2"}


def test_snapshot_item_fields(cluster, tmp_path):
    snap = snapshot_cluster(cluster, "M1", ["M2"], prov=ProvenanceStore(tmp_path))
    m1 = snap.items["M1"]
    assert m1.version == 100
    assert m1.item_type == "journalArticle"
    assert m1.collections == ["COL1"]
    assert m1.tags == [("aztec", 1)]            # (tag, type) pair; default type 0 normalized
    assert m1.citekey == "sandersBasinMexico1979"
    assert "title" in m1.fields and "collections" not in m1.fields  # scalar-only
    assert m1.sha256                              # content hash present
    # secondary default tag type normalizes to 0
    assert snap.items["M2"].tags == [("aztec", 0)]


def test_snapshot_children_and_grandchildren(cluster, tmp_path):
    snap = snapshot_cluster(cluster, "M1", ["M2"], prov=ProvenanceStore(tmp_path))
    assert {n.key for n in snap.notes} == {"N1"}
    assert {a.key for a in snap.attachments} == {"A1", "A2"}

    a1 = next(a for a in snap.attachments if a.key == "A1")
    assert a1.parent_key == "M1" and a1.md5 == "abc123" and a1.filename == "paper.pdf"
    assert [an.key for an in a1.annotations] == ["AN1"]   # annotation grandchild captured

    a2 = next(a for a in snap.attachments if a.key == "A2")
    assert a2.parent_key == "M2" and a2.annotations == []


def test_snapshot_children_of_helper(cluster, tmp_path):
    snap = snapshot_cluster(cluster, "M1", ["M2"], prov=ProvenanceStore(tmp_path))
    assert {c.key for c in snap.children_of("M1")} == {"N1", "A1"}
    assert {c.key for c in snap.children_of("M2")} == {"A2"}


def test_snapshot_writes_prov(cluster, tmp_path):
    prov = ProvenanceStore(tmp_path)
    snap = snapshot_cluster(cluster, "M1", ["M2"], prov=prov)
    recs = prov.query("M1")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["activity"] == "snapshot_cluster"
    assert rec["was_derived_from"] == snap.snapshot_id      # snapshot_id is the rollback index
    assert rec["entity"]["before_blob"]                     # full before-image persisted (reversible)
    assert rec["params"]["n_attachments"] == 2


def test_snapshot_id_unique(cluster, tmp_path):
    prov = ProvenanceStore(tmp_path)
    s1 = snapshot_cluster(cluster, "M1", ["M2"], prov=prov)
    s2 = snapshot_cluster(cluster, "M1", ["M2"], prov=prov)
    assert s1.snapshot_id != s2.snapshot_id

    explicit = snapshot_cluster(cluster, "M1", ["M2"], prov=prov, snapshot_id="fixed-id")
    assert explicit.snapshot_id == "fixed-id"
