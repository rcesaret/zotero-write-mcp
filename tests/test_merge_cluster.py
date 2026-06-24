"""Unit tests for merge_cluster (P2-merge-cluster) — fake reader + fake gateway, no live writes."""
import pytest

from zotero_write_mcp.merge import snapshot_cluster
from zotero_write_mcp.merge_live import merge_cluster, library_item_base
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


class FakeGateway:
    def __init__(self):
        self.calls = []

    def update_item(self, library_id, item_key, data, version, *, library_type="user", retry_on_412=True):
        self.calls.append({"key": item_key, "data": data, "version": version, "retry_on_412": retry_on_412})


def _item(key, version, item_type="journalArticle", **data):
    d = {"key": key, "version": version, "itemType": item_type, **data}
    return {"key": key, "version": version, "data": d}


@pytest.fixture
def setup(tmp_path):
    items = {
        "M1": _item("M1", 100, collections=["C1"], tags=[{"tag": "a", "type": 1}],
                    relations={}, abstractNote=""),
        "M2": _item("M2", 101, collections=["C2"], tags=[{"tag": "b", "type": 1}],
                    relations={}, abstractNote="filled-from-dup"),
    }
    children = {
        "M1": [_item("A1", 110, item_type="attachment", parentItem="M1", md5="x", filename="a.pdf")],
        "M2": [_item("A2", 111, item_type="attachment", parentItem="M2", md5="y", filename="b.pdf")],
    }
    reader = FakeReader(items, children, {}, {"M1": "ck1", "M2": "ck2"})
    snap = snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path))
    return reader, snap


def test_library_item_base():
    assert library_item_base("user", 11056739) == "http://zotero.org/users/11056739/items"
    assert library_item_base("group", 5) == "http://zotero.org/groups/5/items"


def test_merge_cluster_patches(setup):
    reader, snap = setup
    gw = FakeGateway()
    plan = merge_cluster(snap, reader, gw, library_id=11056739)
    assert not plan.drifted
    ops = {(c["op"], c.get("key")) for c in plan.patches}
    assert ("patch-master", "M1") in ops
    assert ("reparent", "A2") in ops          # A2 was under secondary M2
    assert ("reparent", "A1") not in ops      # A1 already under master M1

    mp = next(c for c in gw.calls if c["key"] == "M1")
    assert set(mp["data"]["collections"]) == {"C1", "C2"}                 # union
    assert mp["data"]["tags"] == [{"tag": "a", "type": 1}, {"tag": "b", "type": 1}]
    dc = mp["data"]["relations"]["dc:replaces"]
    assert dc == ["http://zotero.org/users/11056739/items/M2"]            # exact per-secondary URI
    assert mp["version"] == 100                                           # master's current version


def test_merge_cluster_drift_aborts_no_writes(setup):
    reader, snap = setup
    reader._items["M2"]["version"] = 999       # a secondary was edited since the snapshot
    gw = FakeGateway()
    plan = merge_cluster(snap, reader, gw, library_id=11056739)
    assert plan.drifted and "M2" in plan.drift_keys
    assert gw.calls == []                       # NO writes on drift


def test_merge_cluster_child_drift_aborts(setup):
    """MC-1: a concurrent edit to a CHILD (version bump) aborts even though the parent is unchanged."""
    reader, snap = setup
    reader._children["M2"][0]["version"] = 888  # A2 (child of M2) externally re-versioned
    gw = FakeGateway()
    plan = merge_cluster(snap, reader, gw, library_id=11056739)
    assert plan.drifted and "A2" in plan.drift_keys
    assert gw.calls == []


def test_merge_cluster_patches_are_fail_closed(setup):
    """M1/F6/MC-2: every merge PATCH is issued retry_on_412=False (abort-on-conflict, no blind re-apply)."""
    reader, snap = setup
    gw = FakeGateway()
    merge_cluster(snap, reader, gw, library_id=11056739)
    assert gw.calls and all(c["retry_on_412"] is False for c in gw.calls)


def test_merge_cluster_no_smartfill_leaves_master_fields(setup):
    reader, snap = setup
    gw = FakeGateway()
    merge_cluster(snap, reader, gw, library_id=11056739, smart_fill=False)
    mp = next(c for c in gw.calls if c["key"] == "M1")
    assert "abstractNote" not in mp["data"]     # default: never touch master scalar fields


def test_merge_cluster_smartfill_fills_empty_master(setup):
    reader, snap = setup
    gw = FakeGateway()
    merge_cluster(snap, reader, gw, library_id=11056739, smart_fill=True)
    mp = next(c for c in gw.calls if c["key"] == "M1")
    assert mp["data"].get("abstractNote") == "filled-from-dup"   # snapshot-empty + live-empty -> filled


def test_merge_cluster_smartfill_respects_live_population(tmp_path):
    """M-2: a field empty at snapshot but populated LIVE (after snapshot) must NOT be overwritten."""
    items = {
        "M1": _item("M1", 1, abstractNote="", collections=[], tags=[], relations={}),
        "M2": _item("M2", 2, abstractNote="from dup", collections=[], tags=[], relations={}),
    }
    reader = FakeReader(items, {"M1": [], "M2": []}, {}, {"M1": "c", "M2": "c2"})
    snap = snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path))
    # the live master gained an abstractNote AFTER the snapshot (version held same to isolate the M-2 guard)
    reader._items["M1"]["data"]["abstractNote"] = "owner typed this after the snapshot"
    gw = FakeGateway()
    merge_cluster(snap, reader, gw, library_id=11056739, smart_fill=True)
    mp = next(c for c in gw.calls if c["key"] == "M1")
    assert "abstractNote" not in mp["data"]     # M-2: do not clobber the live value
