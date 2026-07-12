"""Unit tests for S5a F5 — backup_before_live (backup.py). Read-only against a fake reader; the only
writes are PROV snapshot records (already the sanctioned merge-safety path) and a local export file."""
import json

from zotero_write_mcp.backup import backup_before_live
from zotero_write_mcp.provenance import ProvenanceStore


class FakeReader:
    def __init__(self, items):
        self._items = items

    def get_item(self, key):
        return self._items[key]

    def get_children(self, key):
        return []

    def get_annotations(self, attachment_key):
        return []

    def get_citekey(self, key):
        return None


def _item(key, version=1, **data):
    d = {"key": key, "version": version, "itemType": "journalArticle", **data}
    return {"key": key, "version": version, "data": d}


def reader():
    return FakeReader({
        "M1": _item("M1", title="t1"),
        "D1": _item("D1", title="d1"),
        "M2": _item("M2", title="t2"),
        "D2": _item("D2", title="d2"),
    })


def test_backup_before_live_snapshots_every_pair(tmp_path):
    prov = ProvenanceStore(tmp_path)
    pairs = [{"master": "M1", "dups": ["D1"]}, {"master": "M2", "dups": ["D2"]}]
    res = backup_before_live(pairs, reader(), prov)
    assert res.pairs_backed_up == 2
    assert len(res.snapshot_ids) == 2
    assert res.blob_missing == []
    assert res.blob_confirmed == res.snapshot_ids


def test_backup_before_live_confirms_blob_present_in_prov(tmp_path):
    prov = ProvenanceStore(tmp_path)
    res = backup_before_live([{"master": "M1", "dups": ["D1"]}], reader(), prov)
    rec = next(r for r in prov.all_records() if r["activity"] == "snapshot_cluster")
    blob = rec["entity"]["before_blob"]
    assert prov.has_blob(blob)
    assert res.snapshot_ids[0] in res.blob_confirmed


def test_backup_before_live_writes_no_zotero_mutation(tmp_path):
    """Only PROV activities appear — never a create/update/delete."""
    prov = ProvenanceStore(tmp_path)
    backup_before_live([{"master": "M1", "dups": ["D1"]}], reader(), prov)
    acts = {r["activity"] for r in prov.all_records()}
    assert acts == {"snapshot_cluster"}


def test_backup_before_live_export_writes_dated_file(tmp_path):
    prov = ProvenanceStore(tmp_path)
    export_dir = tmp_path / "backups"

    def export_fn(keys):
        return {"items": [{"key": k} for k in keys]}

    from datetime import datetime, timezone
    res = backup_before_live(
        [{"master": "M1", "dups": ["D1"]}], reader(), prov,
        export_fn=export_fn, export_dir=str(export_dir),
        now=datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert res.export_path is not None
    assert res.export_item_count == 2
    written = json.loads(open(res.export_path, encoding="utf-8").read())
    assert set(x["key"] for x in written["export"]["items"]) == {"M1", "D1"}
    assert written["snapshot_ids"] == res.snapshot_ids


def test_backup_before_live_export_is_dated_never_overwrites(tmp_path):
    """Two backups run at different times produce two distinct files (version, don't overwrite)."""
    prov = ProvenanceStore(tmp_path)
    export_dir = tmp_path / "backups"

    def export_fn(keys):
        return {"items": []}

    from datetime import datetime, timezone
    res1 = backup_before_live(
        [{"master": "M1", "dups": ["D1"]}], reader(), prov, export_fn=export_fn,
        export_dir=str(export_dir), now=datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc))
    res2 = backup_before_live(
        [{"master": "M2", "dups": ["D2"]}], reader(), prov, export_fn=export_fn,
        export_dir=str(export_dir), now=datetime(2026, 7, 12, 12, 5, 0, tzinfo=timezone.utc))
    assert res1.export_path != res2.export_path
    import os
    assert os.path.exists(res1.export_path) and os.path.exists(res2.export_path)


def test_backup_before_live_no_export_when_export_fn_omitted(tmp_path):
    prov = ProvenanceStore(tmp_path)
    res = backup_before_live([{"master": "M1", "dups": ["D1"]}], reader(), prov)
    assert res.export_path is None
    assert res.export_item_count == 0
