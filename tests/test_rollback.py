"""Unit tests for rollback_merge — all 3 partial states (a / b / c) with a fake gateway."""
import copy

import pytest

from zotero_write_mcp.merge import (
    snapshot_cluster, compute_merge_projection, rollback_merge,
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


class FakeGateway:
    def __init__(self):
        self.calls = []

    def update_item(self, library_id, item_key, data, version, *, library_type="user"):
        self.calls.append({"method": "update", "key": item_key, "data": data, "version": version})

    def create_items(self, library_id, objects, *, library_type="user"):
        self.calls.append({"method": "create", "keys": [o.get("key") for o in objects]})


def _item(key, version, item_type="journalArticle", **data):
    d = {"key": key, "version": version, "itemType": item_type, **data}
    return {"key": key, "version": version, "data": d}


@pytest.fixture
def snap(tmp_path):
    items = {
        "M1": _item("M1", 100, title="t", collections=["C1"], tags=[{"tag": "a", "type": 1}], relations={}),
        "M2": _item("M2", 101, title="t2", collections=["C2"], tags=[{"tag": "b", "type": 1}], relations={}),
    }
    children = {
        "M1": [_item("A1", 110, item_type="attachment", parentItem="M1", md5="x", filename="a.pdf")],
        "M2": [_item("A2", 111, item_type="attachment", parentItem="M2", md5="y", filename="b.pdf")],
    }
    reader = FakeReader(items, children, {}, {"M1": "ck1", "M2": "ck2"})
    return snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path))


def _ops(report):
    return {(o["op"], o.get("key")) for o in report.operations}


def test_state_a_no_op(snap):
    """Observed == snapshot → nothing was written → no-op."""
    observed = copy.deepcopy(snap)
    gw = FakeGateway()
    rep = rollback_merge(snap, observed, gw, library_id=1)
    assert rep.state == "a"
    assert rep.operations == [] and gw.calls == []


def test_state_b_patched_not_deleted(snap):
    """merge_cluster PATCHed (union + reparent) but no DELETE → revert master + reparent A2 back to M2."""
    observed = compute_merge_projection(snap)   # master unioned, A2 reparented to M1, M2 still present
    gw = FakeGateway()
    rep = rollback_merge(snap, observed, gw, library_id=1)
    assert rep.state == "b"
    ops = _ops(rep)
    assert ("revert-master", "M1") in ops
    assert ("reparent", "A2") in ops            # A2 moved M2->M1, must go back to M2
    assert not any(o["op"] in ("untrash", "recreate") for o in rep.operations)
    # A1 was already under M1 (not moved) → no reparent for it
    assert ("reparent", "A1") not in ops


def test_state_b_reparent_targets_original_parent(snap):
    observed = compute_merge_projection(snap)
    gw = FakeGateway()
    rollback_merge(snap, observed, gw, library_id=1)
    reparent = [c for c in gw.calls if c.get("data", {}).get("parentItem")]
    a2 = next(c for c in reparent if c["key"] == "A2")
    assert a2["data"]["parentItem"] == "M2"     # original parent restored


def test_state_c_partial_delete(snap):
    """Some secondaries trashed (deleted:1) → un-trash the subset, THEN revert + reparent."""
    observed = copy.deepcopy(compute_merge_projection(snap))
    observed.items["M2"].json["deleted"] = 1    # M2 was trashed by a partial commit
    gw = FakeGateway()
    rep = rollback_merge(snap, observed, gw, library_id=1)
    assert rep.state == "c"
    ops = _ops(rep)
    assert ("untrash", "M2") in ops
    assert ("revert-master", "M1") in ops
    assert ("reparent", "A2") in ops
    # un-trash must run BEFORE reparent (child needs a parent to return to)
    order = [o["op"] for o in rep.operations]
    assert order.index("untrash") < order.index("reparent")


def test_state_c_absent_secondary_recreated(snap):
    """A hard-gone secondary (absent from observed) is recreated from the snapshot, not un-trashed."""
    observed = copy.deepcopy(compute_merge_projection(snap))
    del observed.items["M2"]
    gw = FakeGateway()
    rep = rollback_merge(snap, observed, gw, library_id=1)
    assert rep.state == "c"
    assert ("recreate", "M2") in _ops(rep)
    create = next(c for c in gw.calls if c["method"] == "create")
    assert "M2" in create["keys"]


def test_revert_master_carries_snapshot_union_fields(snap):
    observed = compute_merge_projection(snap)
    gw = FakeGateway()
    rollback_merge(snap, observed, gw, library_id=1)
    revert = next(c for c in gw.calls if c["key"] == "M1" and "collections" in c.get("data", {}))
    assert set(revert["data"]["collections"]) == {"C1"}          # back to master-only collections
    assert revert["data"]["tags"] == [{"tag": "a", "type": 1}]   # back to master-only tags
    assert revert["version"] == observed.items["M1"].version     # uses current (observed) version
