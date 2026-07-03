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
    fn = _tool_fn("merge_items")
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
