"""Tests for the write gateway v2 (Phase 0: P0-structured-returns + P0-versioning).

Pure-offline: a FakeTransport scripts Web API responses, so the structured-envelope parsing and the
version/concurrency logic are verified with no network and no live Zotero library.
"""
import pytest

from zotero_write_mcp.gateway import (
    BATCH_LIMIT,
    ConcurrencyConflictError,
    GatewayError,
    VersionMissingError,
    WriteGateway,
    WriteResult,
    parse_write_envelope,
)


class FakeResp:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeTransport:
    """Returns scripted responses in order and records every call for assertions."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, path, *, json=None, headers=None, params=None):
        self.calls.append(
            {"method": method, "path": path, "json": json,
             "headers": dict(headers or {}), "params": params or {}})
        if not self._responses:
            raise AssertionError(f"FakeTransport: no scripted response for {method} {path}")
        return self._responses.pop(0)


# ── envelope parsing (P0-structured-returns) ──────────────────────────────────

def test_parse_envelope_extracts_keys_maps_and_version():
    body = {
        "successful": {"0": {"key": "AAAA1111", "version": 42, "data": {"title": "x"}}},
        "success": {"0": "AAAA1111"},
        "unchanged": {"1": "BBBB2222"},
        "failed": {},
    }
    r = parse_write_envelope(body, {"Last-Modified-Version": "42"})
    assert r.item_keys == ["AAAA1111"]
    assert r.unchanged == {"1": "BBBB2222"}
    assert r.last_modified_version == 42
    assert r.all_ok is True


def test_parse_envelope_reports_failures():
    body = {
        "success": {"0": "AAAA1111"},
        "failed": {"1": {"key": "BBBB2222", "code": 412, "message": "conflict"}},
    }
    r = parse_write_envelope(body, {"Last-Modified-Version": "7"})
    assert r.all_ok is False
    assert r.failed_indices == [1]
    assert r.item_keys == ["AAAA1111"]


def test_parse_envelope_item_keys_fall_back_to_successful_objects():
    body = {"successful": {"0": {"key": "CCCC3333"}, "1": {"key": "DDDD4444"}}}
    r = parse_write_envelope(body, {})
    assert r.item_keys == ["CCCC3333", "DDDD4444"]


def test_failed_objects_maps_back_to_inputs():
    objs = [{"t": 0}, {"t": 1}, {"t": 2}]
    r = WriteResult(status_code=200, failed={"1": {"code": 412}})
    assert r.failed_objects(objs) == [{"t": 1}]


# ── create (POST array) ───────────────────────────────────────────────────────

def test_create_items_returns_structured_result():
    t = FakeTransport([FakeResp(200,
        body={"success": {"0": "AAAA1111", "1": "BBBB2222"}, "failed": {}},
        headers={"Last-Modified-Version": "100"})])
    gw = WriteGateway(t)
    r = gw.create_items(11056739, [{"itemType": "journalArticle"}, {"itemType": "book"}])
    assert r.item_keys == ["AAAA1111", "BBBB2222"]
    assert r.last_modified_version == 100
    assert t.calls[0]["method"] == "POST"
    assert t.calls[0]["path"] == "/users/11056739/items"


def test_create_items_rejects_oversized_batch():
    gw = WriteGateway(FakeTransport([]))
    with pytest.raises(GatewayError):
        gw.create_items(1, [{} for _ in range(BATCH_LIMIT + 1)])


# ── update (single PATCH, versioning) — P0-versioning ─────────────────────────

def test_update_item_rejects_version_less_write():
    gw = WriteGateway(FakeTransport([]))
    with pytest.raises(VersionMissingError):
        gw.update_item(1, "AAAA1111", {"title": "x"}, version=None)


def test_update_item_sends_if_unmodified_since_version():
    t = FakeTransport([FakeResp(204, headers={"Last-Modified-Version": "11"})])
    gw = WriteGateway(t)
    r = gw.update_item(1, "AAAA1111", {"title": "x"}, version=10)
    assert t.calls[0]["method"] == "PATCH"
    assert t.calls[0]["headers"]["If-Unmodified-Since-Version"] == "10"
    assert r.item_keys == ["AAAA1111"]
    assert r.last_modified_version == 11


def test_update_item_retries_once_on_412_then_succeeds():
    t = FakeTransport([
        FakeResp(412, headers={"Last-Modified-Version": "20"}),    # first PATCH conflicts
        FakeResp(200, body={"version": 20}),                        # re-GET current version
        FakeResp(204, headers={"Last-Modified-Version": "21"}),    # retry PATCH succeeds
    ])
    gw = WriteGateway(t)
    r = gw.update_item(1, "AAAA1111", {"title": "x"}, version=10)
    assert [c["method"] for c in t.calls] == ["PATCH", "GET", "PATCH"]
    # the retry used the freshly-read version (20), not the stale 10
    assert t.calls[2]["headers"]["If-Unmodified-Since-Version"] == "20"
    assert r.last_modified_version == 21


def test_update_item_raises_when_412_persists():
    t = FakeTransport([
        FakeResp(412),
        FakeResp(200, body={"version": 20}),
        FakeResp(412),
    ])
    gw = WriteGateway(t)
    with pytest.raises(ConcurrencyConflictError):
        gw.update_item(1, "AAAA1111", {"title": "x"}, version=10)


def test_update_item_reads_version_from_data_subobject():
    t = FakeTransport([
        FakeResp(412),
        FakeResp(200, body={"data": {"version": 33}}),   # version nested under data
        FakeResp(204, headers={"Last-Modified-Version": "34"}),
    ])
    gw = WriteGateway(t)
    gw.update_item(1, "K", {"x": 1}, version=5)
    assert t.calls[2]["headers"]["If-Unmodified-Since-Version"] == "33"


# ── delete (versioning; 412 → caller re-resolves) ─────────────────────────────

def test_delete_items_rejects_version_less():
    gw = WriteGateway(FakeTransport([]))
    with pytest.raises(VersionMissingError):
        gw.delete_items(1, ["AAAA1111"], version=None)


def test_delete_items_success_surfaces_version():
    t = FakeTransport([FakeResp(204, headers={"Last-Modified-Version": "55"})])
    gw = WriteGateway(t)
    r = gw.delete_items(1, ["AAAA1111", "BBBB2222"], version=54)
    assert t.calls[0]["method"] == "DELETE"
    assert t.calls[0]["params"]["itemKey"] == "AAAA1111,BBBB2222"
    assert t.calls[0]["headers"]["If-Unmodified-Since-Version"] == "54"
    assert r.item_keys == ["AAAA1111", "BBBB2222"]
    assert r.last_modified_version == 55


def test_delete_items_412_raises_concurrency_conflict_no_blind_retry():
    t = FakeTransport([FakeResp(412, headers={"Last-Modified-Version": "60"})])
    gw = WriteGateway(t)
    with pytest.raises(ConcurrencyConflictError):
        gw.delete_items(1, ["AAAA1111"], version=59)
    assert len(t.calls) == 1  # did NOT blindly re-delete


def test_delete_items_rejects_oversized_batch():
    gw = WriteGateway(FakeTransport([]))
    with pytest.raises(GatewayError):
        gw.delete_items(1, [f"K{i}" for i in range(BATCH_LIMIT + 1)], version=1)
