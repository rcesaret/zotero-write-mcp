"""Tests for P0-legacy-retire: every client mutation routes through the gateway AND lands in PROV.

This is the Phase-0 invariant "no mutation path bypasses the gateway or PROV", verified at the client
layer (the layer all 18 tools call). Offline: a fake transport + a temp PROV store; no network, no
live library.
"""
import pytest

from zotero_write_mcp.gateway import VersionMissingError, WriteGateway
from zotero_write_mcp.provenance import ProvenanceStore


class FakeResp:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeTransport:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, path, *, json=None, headers=None, params=None):
        self.calls.append({"method": method, "path": path, "json": json,
                           "headers": dict(headers or {}), "params": params or {}})
        if not self._responses:
            raise AssertionError(f"FakeTransport: no scripted response for {method} {path}")
        return self._responses.pop(0)


def make_client(monkeypatch, tmp_path, responses):
    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    from zotero_write_mcp.client import ZoteroClient
    transport = FakeTransport(responses)
    prov = ProvenanceStore(tmp_path)
    client = ZoteroClient(gateway=WriteGateway(transport), prov=prov)
    client._library_id = 11056739  # skip local-API detection
    return client, transport, prov


def test_create_items_routes_through_gateway_and_logs_prov(monkeypatch, tmp_path):
    client, t, prov = make_client(monkeypatch, tmp_path, [
        FakeResp(200, body={"success": {"0": "NEWKEY01"}}, headers={"Last-Modified-Version": "100"})])
    result = client.create_items([{"itemType": "journalArticle", "title": "X"}])
    assert t.calls[0]["method"] == "POST"
    assert t.calls[0]["path"] == "/users/11056739/items"
    assert result["success"] == {"0": "NEWKEY01"}            # legacy envelope shape preserved
    recs = prov.query("NEWKEY01")
    assert len(recs) == 1 and recs[0]["activity"] == "create_item"


def test_update_item_routes_through_gateway_and_logs_prov(monkeypatch, tmp_path):
    client, t, prov = make_client(monkeypatch, tmp_path, [
        FakeResp(204, headers={"Last-Modified-Version": "11"})])
    client.update_item("ITEMAAAA", {"title": "New"}, version=10)
    assert t.calls[0]["method"] == "PATCH"
    assert t.calls[0]["headers"]["If-Unmodified-Since-Version"] == "10"
    recs = prov.query("ITEMAAAA")
    assert len(recs) == 1 and recs[0]["activity"] == "update_item"


def test_delete_item_routes_through_gateway_and_logs_prov(monkeypatch, tmp_path):
    client, t, prov = make_client(monkeypatch, tmp_path, [FakeResp(204)])
    client.delete_item("ITEMBBBB", version=5)
    assert t.calls[0]["method"] == "DELETE"
    assert t.calls[0]["params"]["itemKey"] == "ITEMBBBB"
    recs = prov.query("ITEMBBBB")
    assert len(recs) == 1 and recs[0]["activity"] == "delete_item"


def test_client_update_rejects_version_less_and_writes_nothing(monkeypatch, tmp_path):
    client, t, prov = make_client(monkeypatch, tmp_path, [])
    with pytest.raises(VersionMissingError):
        client.update_item("X", {"title": "y"}, version=None)
    assert not t.calls and prov.count() == 0   # rejected before any write or PROV record


def test_no_mutation_bypasses_prov(monkeypatch, tmp_path):
    """Phase-0 invariant: create + update + delete through the client all land in PROV."""
    client, t, prov = make_client(monkeypatch, tmp_path, [
        FakeResp(200, body={"success": {"0": "K1"}}),
        FakeResp(204, headers={"Last-Modified-Version": "2"}),
        FakeResp(204),
    ])
    client.create_items([{"itemType": "book"}])
    client.update_item("K1", {"title": "z"}, version=1)
    client.delete_item("K1", version=2)
    assert prov.count() == 3
    assert {r["activity"] for r in prov.all_records()} == {"create_item", "update_item", "delete_item"}
