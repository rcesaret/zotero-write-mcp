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
