"""Unit tests for shadow_merge — compute + verify + log, NO commit (P1-shadow)."""
import copy

import pytest

from zotero_write_mcp.merge import shadow_merge, compute_merge_projection, snapshot_cluster
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


@pytest.fixture
def reader():
    items = {
        "M1": _item("M1", 1, title="t", collections=["C1"], tags=[{"tag": "a", "type": 1}], relations={}),
        "M2": _item("M2", 2, title="t2", collections=["C2"], tags=[], relations={}),
    }
    children = {"M1": [_item("A1", 3, item_type="attachment", parentItem="M1", md5="x", filename="a.pdf")],
                "M2": []}
    return FakeReader(items, children, {}, {"M1": "ck1", "M2": "ck2"})


def test_shadow_passes_and_logs(reader, tmp_path):
    prov = ProvenanceStore(tmp_path)
    rep = shadow_merge(reader, "M1", ["M2"], prov=prov)
    assert rep.passed
    assert len(rep.integrity.checks) >= 11
    acts = [r["activity"] for r in prov.all_records()]
    assert "snapshot_cluster" in acts and "shadow_merge" in acts
    shadow_rec = next(r for r in prov.all_records() if r["activity"] == "shadow_merge")
    assert shadow_rec["params"]["pass"] is True
    assert shadow_rec["params"]["against"] == "projection"


def test_shadow_never_writes_library(reader, tmp_path):
    """Shadow logs only read-derived PROV activities — never a create/update/delete mutation."""
    prov = ProvenanceStore(tmp_path)
    shadow_merge(reader, "M1", ["M2"], prov=prov)
    mutating = {"create_item", "update_item", "delete_item", "merge_cluster", "commit_merge"}
    assert not (mutating & {r["activity"] for r in prov.all_records()})


def test_shadow_logs_failure_against_observed(reader, tmp_path):
    """When given a corrupted observed state, shadow reports + LOGS the failure (no exception)."""
    prov = ProvenanceStore(tmp_path)
    snap = snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path / "x"))
    bad = copy.deepcopy(compute_merge_projection(snap))
    bad.items["M1"].citekey = "tampered"          # break citekey preservation
    rep = shadow_merge(reader, "M1", ["M2"], prov=prov, observed=bad)
    assert not rep.passed
    rec = next(r for r in prov.all_records() if r["activity"] == "shadow_merge")
    assert rec["params"]["pass"] is False
    assert "citekey-preservation" in rec["params"]["failed"]
    assert rec["params"]["against"] == "observed"
