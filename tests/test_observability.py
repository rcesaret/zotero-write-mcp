"""Unit tests for observability (P2-observability) — query_provenance + daily_report + freshness guard."""
from datetime import datetime, timezone

import pytest

from zotero_write_mcp.observability import (
    query_provenance, daily_report, latest_daily_report, observability_is_fresh,
)
from zotero_write_mcp.provenance import ProvenanceStore


def _seed(tmp_path):
    p = ProvenanceStore(tmp_path)
    p.record(activity="snapshot_cluster", item_key="M1", before={"snap": 1},
             snapshot_id="snap-1", ts="2026-06-24T09:59:00+00:00")
    p.record(activity="shadow_merge", item_key="M1", params={"pass": True},
             ts="2026-06-24T10:00:00+00:00")
    p.record(activity="shadow_merge", item_key="M2", params={"pass": False, "failed": ["x"]},
             ts="2026-06-24T10:01:00+00:00")
    p.record(activity="commit_merge", item_key="M1", before={"a": 1}, after={"a": 2},
             snapshot_id="snap-1", ts="2026-06-24T10:02:00+00:00")
    return p


def test_query_provenance_history_and_reversibility(tmp_path):
    p = _seed(tmp_path)
    h = query_provenance(p, "M1")
    acts = [e["activity"] for e in h]
    assert acts == ["snapshot_cluster", "shadow_merge", "commit_merge"]   # append order
    snap = next(e for e in h if e["activity"] == "snapshot_cluster")
    assert snap["reversibility"]["snapshot_id"] == "snap-1"
    assert snap["reversibility"]["before_blob"]                            # before-image persisted
    commit = next(e for e in h if e["activity"] == "commit_merge")
    assert commit["reversibility"]["before_blob"] and commit["reversibility"]["after_blob"]


def test_query_provenance_empty(tmp_path):
    assert query_provenance(ProvenanceStore(tmp_path), "NOPE") == []


def test_daily_report_metrics(tmp_path):
    p = _seed(tmp_path)
    rep = daily_report(p, ts="2026-06-24T12:00:00+00:00")
    assert rep["verify_total"] == 2 and rep["verify_passed"] == 1
    assert rep["verify_pass_rate"] == 0.5
    assert rep["merges_committed"] == 1
    assert rep["status"] == "degraded"                                    # F3: 0.5 < default floor 1.0
    assert rep["pass_rate_floor"] == 1.0
    assert len(rep["sampled_audit"]) == 1 and rep["sampled_audit"][0]["item_key"] == "M1"
    assert rep["sampled_audit"][0]["after_blob"]                          # audit can reconstruct the merge
    assert latest_daily_report(p)["activity"] == "daily_report"           # marker persisted


def test_daily_report_clean_is_ok(tmp_path):
    """All recent verifies pass -> rate 1.0 -> status ok."""
    p = ProvenanceStore(tmp_path)
    p.record(activity="shadow_merge", item_key="A", params={"pass": True}, ts="2026-06-24T10:00:00+00:00")
    p.record(activity="commit_merge", item_key="B", params={"pass": True}, ts="2026-06-24T10:01:00+00:00")
    rep = daily_report(p, ts="2026-06-24T12:00:00+00:00")
    assert rep["verify_pass_rate"] == 1.0 and rep["status"] == "ok"


def test_freshness_rejects_degraded_report(tmp_path):
    """F3/C-2: a report that RAN but recorded a sub-floor verify-pass-rate is degraded -> NOT fresh
    (so commit_merge fails closed even though the report is recent)."""
    p = _seed(tmp_path)                                                   # rate 0.5
    daily_report(p, ts="2026-06-24T12:00:00+00:00")                       # default floor 1.0 -> degraded
    now = datetime(2026, 6, 24, 12, 1, tzinfo=timezone.utc)
    assert not observability_is_fresh(p, window_seconds=48 * 3600, now=now)


def test_daily_report_no_verifies(tmp_path):
    rep = daily_report(ProvenanceStore(tmp_path), ts="2026-06-24T12:00:00+00:00")
    assert rep["verify_total"] == 0 and rep["verify_pass_rate"] is None and rep["status"] == "ok"


def test_freshness_fresh_then_stale(tmp_path):
    p = _seed(tmp_path)
    daily_report(p, ts="2026-06-24T12:00:00+00:00", pass_rate_floor=0.0)  # floor 0 -> ok (isolate age logic)
    fresh_now = datetime(2026, 6, 24, 12, 30, tzinfo=timezone.utc)        # 30 min old
    assert observability_is_fresh(p, window_seconds=48 * 3600, now=fresh_now)
    stale_now = datetime(2026, 6, 27, 13, 0, tzinfo=timezone.utc)         # ~3 days old
    assert not observability_is_fresh(p, window_seconds=48 * 3600, now=stale_now)


def test_freshness_absent_is_not_fresh(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    assert not observability_is_fresh(ProvenanceStore(tmp_path), window_seconds=48 * 3600, now=now)


def test_freshness_status_not_ok_is_not_fresh(tmp_path):
    p = ProvenanceStore(tmp_path)
    p.record(activity="daily_report", agent="observability", params={"status": "error"},
             ts="2026-06-24T12:00:00+00:00")
    now = datetime(2026, 6, 24, 12, 1, tzinfo=timezone.utc)
    assert not observability_is_fresh(p, window_seconds=48 * 3600, now=now)


def test_freshness_future_marker_rejected(tmp_path):
    """A report timestamped in the future (clock skew) is not trusted as fresh."""
    p = ProvenanceStore(tmp_path)
    daily_report(p, ts="2026-06-24T18:00:00+00:00")
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)               # marker is 6h in the future
    assert not observability_is_fresh(p, window_seconds=48 * 3600, now=now)
