"""WebClusterReader read-path edge cases — the live merge_live reader used by snapshot_cluster.

Regression for the 2026-06 exit-gate finding: Zotero's /items/<key>/children endpoint returns HTTP 400
("can only be called on PDF, EPUB, and snapshot attachments") for any other attachment type, which was
aborting snapshot_cluster on real clusters that contain a non-annotatable attachment."""
from zotero_write_mcp.merge_live import WebClusterReader


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _HTTPError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = _Resp(status_code)


class _FakeClient:
    """Mimics ZoteroClient._web_get; raises on a non-annotatable attachment's /children."""

    def __init__(self, children_by_key=None, error_keys=None):
        self.children_by_key = children_by_key or {}
        self.error_keys = error_keys or {}        # key -> status_code to raise

    def _web_get(self, path, params=None):
        key = path.strip("/").split("/")[3] if path.endswith("/children") else None
        if path.endswith("/children"):
            if key in self.error_keys:
                raise _HTTPError(self.error_keys[key])
            return self.children_by_key.get(key, [])
        return {}


def test_get_annotations_swallows_400_non_pdf_attachment():
    """A non-PDF/EPUB/snapshot attachment 400s on /children -> no annotations, do NOT abort the snapshot."""
    reader = WebClusterReader(_FakeClient(error_keys={"ATTACH1": 400}), library_id=1)
    assert reader.get_annotations("ATTACH1") == []


def test_get_annotations_returns_annotations_on_success():
    reader = WebClusterReader(_FakeClient(children_by_key={"PDF1": [
        {"data": {"itemType": "annotation", "key": "AN1"}},
        {"data": {"itemType": "note", "key": "N1"}},
    ]}), library_id=1)
    assert [a["data"]["key"] for a in reader.get_annotations("PDF1")] == ["AN1"]


def test_get_annotations_reraises_non_400():
    """A real error (e.g. 500) must NOT be silently swallowed — only the documented 400 is benign."""
    reader = WebClusterReader(_FakeClient(error_keys={"ATTACH2": 500}), library_id=1)
    try:
        reader.get_annotations("ATTACH2")
        assert False, "expected the 500 to propagate"
    except Exception as exc:
        assert getattr(getattr(exc, "response", None), "status_code", None) == 500


# ── C.1: WebClusterReader.get_citekey — the live source for verify check #11 ──────────────────────
# Empirically (S0 web-API survey, library 11056739, 40 items): `data.citationKey` is present on the
# Web API for 100% of items; a pinned `Citation Key:` line in `extra` for ~27%. get_citekey prefers
# the pinned extra key (stable, owner-locked route), else the citationKey field, else None (an
# honestly keyless item is None==None under check #11 — never a fabricated key).


class _ItemClient:
    """Mimics ZoteroClient._web_get for item GETs: returns a Web-API envelope wrapping the given data
    dict. `/children` GETs return [] (single-item clusters carry no children here)."""

    def __init__(self, data_by_key):
        self.data_by_key = data_by_key

    def _web_get(self, path, params=None):
        key = path.rstrip("/").split("/")[-1]
        if path.endswith("/children"):
            return []
        data = dict(self.data_by_key[key])
        data.setdefault("itemType", "journalArticle")
        data.setdefault("key", key)
        return {"key": key, "version": data.get("version", 5), "data": data}


def test_get_citekey_prefers_pinned_extra_line():
    reader = WebClusterReader(_ItemClient({
        "K": {"extra": "Citation Key: pinnedKey\ntex.ids: aliasA, aliasB", "citationKey": "computedKey"},
    }), library_id=1)
    assert reader.get_citekey("K") == "pinnedKey"


def test_get_citekey_falls_back_to_citationKey_field():
    reader = WebClusterReader(_ItemClient({
        "K": {"extra": "", "citationKey": "computedKey"},
    }), library_id=1)
    assert reader.get_citekey("K") == "computedKey"


def test_get_citekey_none_when_no_source():
    # A tex.ids-only extra (aliases, no primary key) and no citationKey field → honestly keyless.
    reader = WebClusterReader(_ItemClient({"K": {"extra": "tex.ids: only, aliases"}}), library_id=1)
    assert reader.get_citekey("K") is None


def test_check11_bites_when_master_citekey_changes():
    """A live-shaped merge whose post-PATCH master carries a DIFFERENT pinned Citation Key than the
    snapshot captured must FAIL verify check #11. RED against the old `get_citekey -> None` (None==None
    passed #11 for every live merge); GREEN once get_citekey reads a real key. The scalar-field check #3
    co-fires (the `extra` string differs), so this test discriminates specifically on the #11 name."""
    from zotero_write_mcp.merge import build_cluster, verify_merge
    snap_reader = WebClusterReader(_ItemClient({
        "M": {"extra": "Citation Key: originalKey", "title": "Basin of Mexico"}}), library_id=1)
    obs_reader = WebClusterReader(_ItemClient({
        "M": {"extra": "Citation Key: TAMPEREDKEY", "title": "Basin of Mexico"}}), library_id=1)
    snap = build_cluster(snap_reader, "M", [])
    observed = build_cluster(obs_reader, "M", [])
    report = verify_merge(snap, observed)
    failed_names = {c.name for c in report.failed}
    assert report.passed is False
    assert "citekey-preservation" in failed_names
