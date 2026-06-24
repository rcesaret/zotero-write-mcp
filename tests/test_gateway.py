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


# ── rate governor: Backoff / Retry-After (P0-batch-concurrency) ────────────────

def test_governor_waits_out_backoff_before_next_request():
    sleeps = []
    t = FakeTransport([
        FakeResp(200, body={}, headers={"Backoff": "5"}),  # server asks us to pause
        FakeResp(200, body={}),
    ])
    gw = WriteGateway(t, sleep=sleeps.append, monotonic=lambda: 0.0)
    gw.create_items(1, [{"itemType": "book"}])   # response sets resume_at = 0 + 5
    gw.create_items(1, [{"itemType": "book"}])   # must wait ~5 before sending
    assert sleeps == [5.0]


def test_governor_retries_on_429_honoring_retry_after():
    sleeps = []
    t = FakeTransport([
        FakeResp(429, headers={"Retry-After": "3"}),
        FakeResp(200, body={"success": {"0": "AAAA1111"}}),
    ])
    gw = WriteGateway(t, sleep=sleeps.append, monotonic=lambda: 0.0)
    r = gw.create_items(1, [{"itemType": "book"}])
    assert sleeps == [3]
    assert len(t.calls) == 2
    assert r.item_keys == ["AAAA1111"]


# ── chunking + merge (P0-batch-concurrency) ───────────────────────────────────

def test_create_items_chunked_splits_and_merges_in_order():
    t = FakeTransport([
        FakeResp(200, body={"success": {"0": "K0", "1": "K1"}}, headers={"Last-Modified-Version": "201"}),
        FakeResp(200, body={"success": {"0": "K2", "1": "K3"}}, headers={"Last-Modified-Version": "203"}),
        FakeResp(200, body={"success": {"0": "K4"}}, headers={"Last-Modified-Version": "205"}),
    ])
    gw = WriteGateway(t, batch_limit=2)
    objs = [{"n": i} for i in range(5)]
    r = gw.create_items_chunked(1, objs)
    assert [c["method"] for c in t.calls] == ["POST", "POST", "POST"]   # 3 chunks of 2,2,1
    assert r.item_keys == ["K0", "K1", "K2", "K3", "K4"]
    assert r.last_modified_version == 205   # last chunk's version


def test_create_items_chunked_reindexes_failures_to_global_positions():
    t = FakeTransport([
        FakeResp(200, body={"success": {"0": "KA"}, "failed": {"1": {"code": 412}}}),
        FakeResp(200, body={"success": {"0": "KC"}, "failed": {"1": {"code": 400}}}),
    ])
    gw = WriteGateway(t, batch_limit=2)
    objs = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    r = gw.create_items_chunked(1, objs)
    assert r.failed_indices == [1, 3]                       # b (chunk0[1]) and d (chunk1[1]=global 3)
    assert r.failed_objects(objs) == [{"id": "b"}, {"id": "d"}]


# ── partial-failure retry (P0-partial-retry) ──────────────────────────────────

def test_resubmit_failed_resubmits_only_the_failed_inputs():
    submit = FakeTransport([FakeResp(200, body={"success": {"0": "NEWKEY"}})])
    gw = WriteGateway(submit, batch_limit=50)
    objs = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    prior = WriteResult(status_code=200, failed={"1": {"code": 412}})  # only b failed
    r = gw.resubmit_failed(1, objs, prior)
    # exactly one POST, carrying ONLY the failed object b
    assert len(submit.calls) == 1
    assert submit.calls[0]["json"] == [{"id": "b"}]
    assert r.item_keys == ["NEWKEY"]


def test_resubmit_failed_is_noop_when_nothing_failed():
    t = FakeTransport([])
    gw = WriteGateway(t)
    r = gw.resubmit_failed(1, [{"id": "a"}], WriteResult(status_code=200))
    assert r.all_ok and not t.calls


# ── PUT guard (P0-put-guard) ──────────────────────────────────────────────────

def test_replace_item_forbidden_without_complete_object_flag():
    t = FakeTransport([])
    gw = WriteGateway(t)
    with pytest.raises(GatewayError):
        gw.replace_item(1, "AAAA1111", {"itemType": "book"})  # complete_object defaults False
    assert not t.calls   # never touched the API


def test_replace_item_with_flag_regets_then_puts_with_fresh_version():
    t = FakeTransport([
        FakeResp(200, body={"version": 9}),                       # re-GET fresh version
        FakeResp(204, headers={"Last-Modified-Version": "10"}),  # PUT succeeds
    ])
    gw = WriteGateway(t)
    r = gw.replace_item(1, "AAAA1111", {"itemType": "book", "title": "Full"}, complete_object=True)
    assert [c["method"] for c in t.calls] == ["GET", "PUT"]
    assert t.calls[1]["headers"]["If-Unmodified-Since-Version"] == "9"   # used the fresh version
    assert r.last_modified_version == 10


# ── delete chunking threads the version forward ───────────────────────────────

def test_delete_items_chunked_threads_library_version():
    t = FakeTransport([
        FakeResp(204, headers={"Last-Modified-Version": "101"}),
        FakeResp(204, headers={"Last-Modified-Version": "102"}),
    ])
    gw = WriteGateway(t, batch_limit=2)
    r = gw.delete_items_chunked(1, ["k0", "k1", "k2"], version=100)
    assert t.calls[0]["headers"]["If-Unmodified-Since-Version"] == "100"
    assert t.calls[1]["headers"]["If-Unmodified-Since-Version"] == "101"   # advanced after chunk 1
    assert r.item_keys == ["k0", "k1", "k2"]
    assert r.last_modified_version == 102


# ── library prefix / group parameterization (P0-prefix-stub; G6 deferred) ─────

def test_library_prefix_user_and_group():
    from zotero_write_mcp.gateway import library_prefix
    assert library_prefix("user", 11056739) == "/users/11056739"
    assert library_prefix("group", 42) == "/groups/42"
    with pytest.raises(GatewayError):
        library_prefix("team", 1)


def test_create_items_uses_group_prefix_when_requested():
    t = FakeTransport([FakeResp(200, body={"success": {"0": "G1"}})])
    WriteGateway(t).create_items(42, [{"itemType": "book"}], library_type="group")
    assert t.calls[0]["path"] == "/groups/42/items"


def test_default_library_type_is_user():
    t = FakeTransport([FakeResp(200, body={"success": {"0": "U1"}})])
    WriteGateway(t).create_items(7, [{"itemType": "book"}])
    assert t.calls[0]["path"] == "/users/7/items"


def test_delete_items_uses_group_prefix_when_requested():
    t = FakeTransport([FakeResp(204, headers={"Last-Modified-Version": "5"})])
    WriteGateway(t).delete_items(42, ["K1"], version=4, library_type="group")
    assert t.calls[0]["path"] == "/groups/42/items"


def test_preflight_key_user_noop_group_raises():
    gw = WriteGateway(FakeTransport([]))
    assert gw.preflight_key("user", 1) is None          # personal scope: no-op
    with pytest.raises(GatewayError):
        gw.preflight_key("group", 1)                     # G6 deferred: stubbed-inert


def test_replace_item_reget_and_put_use_user_prefix_path():
    # guards the prefix wiring: re-GET + PUT must hit /users/.../items/..., not a malformed path
    t = FakeTransport([FakeResp(200, body={"version": 9}),
                       FakeResp(204, headers={"Last-Modified-Version": "10"})])
    WriteGateway(t).replace_item(11056739, "ABCD1234", {"itemType": "book"}, complete_object=True)
    assert t.calls[0]["path"] == "/users/11056739/items/ABCD1234"   # GET
    assert t.calls[1]["path"] == "/users/11056739/items/ABCD1234"   # PUT
