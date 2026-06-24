"""Server wiring: the Phase-2 gated merge tools are registered and the legacy merge_items is retired.

Skipped when fastmcp isn't installed (the ephemeral test venv omits it; this runs in the engine venv)."""
import pytest

pytest.importorskip("fastmcp")

from zotero_write_mcp import server  # noqa: E402

GATED = ["snapshot_cluster", "merge_cluster", "commit_merge", "rollback_merge",
         "dedup_scan", "query_provenance", "merge_health_report"]


def test_gated_merge_tools_registered():
    assert all(hasattr(server, t) for t in GATED), [t for t in GATED if not hasattr(server, t)]


def test_merge_items_retired_refuses():
    """The legacy ungated merge tool refuses + redirects (its refusal returns before any get_client())."""
    fn = getattr(server.merge_items, "fn", None) or server.merge_items
    out = fn("PRIMARY", "SECONDARY", confirm=True)
    assert "RETIRED" in out and "commit_merge" in out
