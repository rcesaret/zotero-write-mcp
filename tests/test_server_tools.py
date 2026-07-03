"""Server wiring: the Phase-2 gated merge tools are registered and the legacy merge_items is retired.

Skipped when fastmcp isn't installed (the ephemeral test venv omits it; this runs in the engine venv)."""
import json

import pytest

pytest.importorskip("fastmcp")

from zotero_write_mcp import server  # noqa: E402

GATED = ["snapshot_cluster", "merge_cluster", "commit_merge", "rollback_merge",
         "dedup_scan", "query_provenance", "merge_health_report"]


def _tool_fn(name):
    """The underlying function of a @mcp.tool() (fastmcp wraps it in a .fn attribute)."""
    t = getattr(server, name)
    return getattr(t, "fn", None) or t


def test_gated_merge_tools_registered():
    assert all(hasattr(server, t) for t in GATED), [t for t in GATED if not hasattr(server, t)]


def test_merge_items_retired_refuses():
    """The legacy ungated merge tool refuses + redirects (its refusal returns before any get_client())."""
    fn = getattr(server.merge_items, "fn", None) or server.merge_items
    out = fn("PRIMARY", "SECONDARY", confirm=True)
    assert "RETIRED" in out and "commit_merge" in out


# ── C.2: orphan-commit crash recovery at startup + on-demand tool + no-snapshot-blob alert ────────

def test_get_client_runs_startup_reconcile(monkeypatch):
    """get_client() runs F4 crash-recovery (reconcile_orphan_commits) once at client init, wired with the
    client's own prov/gateway/library_id, and stores the summary."""
    calls = []

    class _FakeClient:
        prov = "PROV"
        gateway = "GW"
        library_id = 42

    monkeypatch.setattr(server, "ZoteroClient", lambda: _FakeClient())
    monkeypatch.setattr(server, "WebClusterReader", lambda c, lib: ("READER", c, lib))

    def _fake_reconcile(prov, reader, gateway, *, library_id, library_type="user"):
        calls.append((prov, gateway, library_id))
        return [{"snapshot_id": "S1", "status": "reconciled"}]

    monkeypatch.setattr(server, "_eng_reconcile", _fake_reconcile)
    monkeypatch.setattr(server, "_client", None)
    server.get_client()
    assert calls == [("PROV", "GW", 42)]
    assert server._startup_reconcile["orphans_found"] == 1
    assert server._startup_reconcile["reconciled"] == 1


def test_reconcile_orphans_tool_surfaces_no_snapshot_blob(monkeypatch):
    """The reconcile_orphans tool reports a missing-snapshot-blob orphan LOUDLY (payload alert), never a
    silent skip."""

    class _FakeClient:
        prov = "PROV"
        gateway = "GW"
        library_id = 7

    monkeypatch.setattr(server, "ZoteroClient", lambda: _FakeClient())
    monkeypatch.setattr(server, "WebClusterReader", lambda c, lib: "READER")
    monkeypatch.setattr(server, "_eng_reconcile",
                        lambda *a, **k: [{"snapshot_id": "GHOST", "status": "no-snapshot-blob"}])
    monkeypatch.setattr(server, "_client", None)
    out = json.loads(_tool_fn("reconcile_orphans")())
    assert out["orphans_found"] == 1
    assert out["no_snapshot_blob"] == ["GHOST"]
    assert out["alert"] and "human review" in out["alert"].lower()


# ── C.4: field_sources + expected_master_version wired onto merge_cluster / commit_merge tools ─────

def _stub_client(monkeypatch, lib_id=9):
    """Stub out the live client + reader + snapshot load + startup reconcile so a merge tool runs
    offline against a monkeypatched engine fn (no network, no library)."""

    class _FakeClient:
        prov = "PROV"
        gateway = "GW"
        library_id = lib_id

    monkeypatch.setattr(server, "ZoteroClient", lambda: _FakeClient())
    monkeypatch.setattr(server, "WebClusterReader", lambda c, lib: "READER")
    monkeypatch.setattr(server, "_eng_load_snapshot", lambda prov, sid: object())   # non-None snapshot
    monkeypatch.setattr(server, "_eng_reconcile", lambda *a, **k: [])               # startup no-op
    monkeypatch.setattr(server, "_client", None)


def test_merge_cluster_threads_field_sources(monkeypatch):
    captured = {}

    class _Plan:
        drifted, drift_keys, patches, master_version = False, [], [], 77

    _stub_client(monkeypatch)
    monkeypatch.setattr(server, "_eng_merge", lambda *a, **k: (captured.update(k) or _Plan()))
    out = json.loads(_tool_fn("merge_cluster")("M", ["S"], "SID", smart_fill=True,
                                               field_sources={"title": "S"}))
    assert captured["field_sources"] == {"title": "S"}
    assert captured["smart_fill"] is True
    assert out["master_version"] == 77          # the version to feed commit_merge's expected_master_version


def test_commit_merge_threads_field_sources_and_expected_version(monkeypatch):
    captured = {}

    class _Res:
        mode, reason, verify_passed, trashed, rollback = "shadow", "", True, [], None

    _stub_client(monkeypatch)
    monkeypatch.setattr(server, "_eng_commit", lambda *a, **k: (captured.update(k) or _Res()))
    _tool_fn("commit_merge")("M", "SID", field_sources={"date": "S"}, expected_master_version=77)
    assert captured["field_sources"] == {"date": "S"}
    assert captured["expected_master_version"] == 77


def test_commit_merge_param_defaults_unchanged(monkeypatch):
    captured = {}

    class _Res:
        mode, reason, verify_passed, trashed, rollback = "shadow", "", True, [], None

    _stub_client(monkeypatch)
    monkeypatch.setattr(server, "_eng_commit", lambda *a, **k: (captured.update(k) or _Res()))
    _tool_fn("commit_merge")("M", "SID")
    assert captured["field_sources"] is None
    assert captured["expected_master_version"] is None


# ── C.5: prune — linked-attach hard-refused at the engine; imported-only default ──────────────────

def test_attach_file_linked_hard_refuses():
    """S0 C.5: the linked-attach tool hard-refuses at the engine (imported-only, closes the raw-MCP
    bypass) — the refusal returns BEFORE any get_client(), so no live client is needed."""
    out = _tool_fn("attach_file_linked")("ITEM", "C:/does/not/matter.pdf")
    assert "DISABLED" in out and "attach_file_imported" in out


def test_bulk_link_files_rejects_linked_mode_and_defaults_imported():
    """S0 C.5: bulk_link_files rejects mode='linked' and the default is now 'imported' (poka-yoke)."""
    import inspect
    out = _tool_fn("bulk_link_files")([{"file_path": "x", "item_key": "y"}], mode="linked")
    assert "Only 'imported'" in out
    assert inspect.signature(_tool_fn("bulk_link_files")).parameters["mode"].default == "imported"


def test_create_linked_file_attachment_raises_at_client():
    """S0 C.5: the deepest layer (client) hard-refuses too — a direct caller cannot create a linked
    attachment via the ZoteroClient method."""
    import pytest as _pytest
    from zotero_write_mcp.client import ZoteroClient
    with _pytest.raises(RuntimeError, match="DISABLED"):
        ZoteroClient.create_linked_file_attachment(object(), "P", "f.pdf", "t", "application/pdf")


# ── S3: validate_record — read-only, zero Zotero writes, logs a read-only PROV record ─────────────

class _FakeProv:
    """Captures every .record() call; exposes NO write-shaped method at all — if validate_record ever
    tried to create/update/delete an item through this fake it would AttributeError, not silently
    succeed, which is exactly the assertion we want for 'validate_record makes zero Zotero writes'."""

    def __init__(self):
        self.calls = []

    def record(self, **kw):
        self.calls.append(kw)
        return kw


class _FakeGathered:
    def __init__(self, records, evidence=None, available=None, answered=None):
        self.records = records
        self.evidence = evidence or []
        self.available = available or []
        self.answered = answered or []


def _stub_validate_client(monkeypatch, prov):
    class _FakeClient:
        def __init__(self):
            self.prov = prov
    monkeypatch.setattr(server, "ZoteroClient", _FakeClient)
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_eng_reconcile", lambda *a, **k: [])   # startup no-op


def test_validate_record_registered():
    assert hasattr(server, "validate_record")


def test_validate_record_is_pure_read_makes_zero_zotero_writes(monkeypatch):
    """The FakeClient/FakeProv carry NO create/update/delete method — calling validate_record must
    never attempt one (would AttributeError, which we'd see as a test failure, not a silent write)."""
    prov = _FakeProv()
    _stub_validate_client(monkeypatch, prov)
    monkeypatch.setattr(server._eng_sources, "default_authorities", lambda: [])
    monkeypatch.setattr(server._eng_sources, "gather_by_search",
                        lambda record, authorities: _FakeGathered([]))
    monkeypatch.setattr(server._eng_validation, "load_calibration",
                        lambda: server._eng_validation.DEFAULT_CALIBRATION)

    out = json.loads(_tool_fn("validate_record")(
        item_type="journalArticle", title="Some Title",
        creators=[{"creatorType": "author", "lastName": "Smith"}], date="2020",
    ))
    assert set(out.keys()) >= {"p", "decision", "evidence", "conflicts"}
    assert out["decision"] in ("accept", "flag", "reject")


def test_validate_record_logs_exactly_one_readonly_prov_informed_by(monkeypatch):
    prov = _FakeProv()
    _stub_validate_client(monkeypatch, prov)
    monkeypatch.setattr(server._eng_sources, "default_authorities", lambda: [])
    monkeypatch.setattr(server._eng_sources, "gather_by_doi",
                        lambda doi, authorities: _FakeGathered(
                            [{"source": "crossref", "title": "Some Title", "date": "2020",
                              "creators": [{"lastName": "Smith"}], "doi": "10.1/x"}],
                            evidence=["crossref: ok"], available=["crossref"], answered=["crossref"]))
    monkeypatch.setattr(server._eng_validation, "load_calibration",
                        lambda: server._eng_validation.DEFAULT_CALIBRATION)

    _tool_fn("validate_record")(
        item_type="journalArticle", title="Some Title",
        creators=[{"creatorType": "author", "lastName": "Smith"}], date="2020", doi="10.1/x",
    )
    assert len(prov.calls) == 1
    rec = prov.calls[0]
    assert rec["activity"] == "validate_record"
    # A read-only "informed-by" record: before/after are never passed, so this is unambiguously NOT
    # a mutation entry (json_sha256 on both stays null in the real ProvenanceStore).
    assert "before" not in rec and "after" not in rec
    assert "identity_sha256" in rec["params"]
    assert rec["params"]["decision"] in ("accept", "flag", "reject")


def test_validate_record_degrades_cleanly_with_no_authorities_available(monkeypatch):
    """Every authority unavailable (e.g. no OPENALEX_API_KEY, DNS down, ...) -> never crashes, never
    auto-accepts, routes to flag with an honest evidence trail (PLAN1 SS4 degraded path)."""
    prov = _FakeProv()
    _stub_validate_client(monkeypatch, prov)
    monkeypatch.setattr(server._eng_sources, "default_authorities", lambda: [])
    monkeypatch.setattr(server._eng_sources, "gather_by_search",
                        lambda record, authorities: _FakeGathered(
                            [], evidence=["openalex: unavailable (no key)", "crossref: 0 candidate(s)"]))
    monkeypatch.setattr(server._eng_validation, "load_calibration",
                        lambda: server._eng_validation.DEFAULT_CALIBRATION)

    out = json.loads(_tool_fn("validate_record")(
        item_type="journalArticle", title="Obscure Paper With No Hits", creators=[], date="2020",
    ))
    assert out["decision"] != "accept"
    assert any("unavailable" in e for e in out["evidence"])
