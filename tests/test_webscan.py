"""Unit tests for the shared read-only Web-API pager (webscan.py) — a fake httpx-shaped client, no
network. Mirrors the paging/backoff behavior scripts/phase2_apply_reconciled.py already proved live."""
from zotero_write_mcp.webscan import web_items


class _Resp:
    def __init__(self, batch, status_code=200, headers=None):
        self._batch = batch
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._batch

    def raise_for_status(self):
        pass


class FakeHttpxClient:
    def __init__(self, pages):
        self._pages = pages   # list of batches, in call order
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append(dict(params))
        idx = len(self.calls) - 1
        batch = self._pages[idx] if idx < len(self._pages) else []
        return _Resp(batch)


class FakeClient:
    def __init__(self, pages):
        self.web_url = "https://api.zotero.org"
        self.library_id = 11056739
        self._web_headers = {"Zotero-API-Key": "x"}
        self._client = FakeHttpxClient(pages)


def _item(key):
    return {"key": key, "data": {"key": key}}


def test_web_items_pages_until_short_batch():
    pages = [[_item(f"K{i}") for i in range(3)], [_item("K3")]]
    client = FakeClient(pages)
    items = web_items(client, item_type="-attachment", page=3)
    assert [it["key"] for it in items] == ["K0", "K1", "K2", "K3"]
    assert client._client.calls[0]["start"] == 0
    assert client._client.calls[1]["start"] == 3


def test_web_items_empty_library():
    client = FakeClient([[]])
    assert web_items(client) == []


def test_web_items_single_short_page_stops():
    pages = [[_item("K0"), _item("K1")]]
    client = FakeClient(pages)
    items = web_items(client, page=100)
    assert len(items) == 2
    assert len(client._client.calls) == 1


def test_web_items_honors_item_type_param():
    client = FakeClient([[]])
    web_items(client, item_type="attachment")
    assert client._client.calls[0]["itemType"] == "attachment"


def test_web_items_include_trashed_adds_param():
    client = FakeClient([[]])
    web_items(client, include_trashed=True)
    assert client._client.calls[0]["includeTrashed"] == 1


def test_web_items_excludes_trashed_by_default():
    client = FakeClient([[]])
    web_items(client)
    assert "includeTrashed" not in client._client.calls[0]
