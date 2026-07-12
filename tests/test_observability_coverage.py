"""Unit tests for S5a F2 — prov_coverage_report (observability.py). Reuses the _seed fixture shape
from test_observability.py to keep the two aggregations directly comparable."""
from zotero_write_mcp.observability import prov_coverage_report, acknowledge_verify_failure
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


def test_prov_coverage_report_basic_counts(tmp_path):
    p = _seed(tmp_path)
    rep = prov_coverage_report(p)
    assert rep["total_records"] == 4
    assert rep["by_activity"] == {
        "snapshot_cluster": 1, "shadow_merge": 2, "commit_merge": 1,
    }
    assert rep["verify_total"] == 2
    assert rep["verify_passed"] == 1
    assert rep["verify_pass_rate"] == 0.5
    assert rep["merges_committed"] == 1
    assert rep["verify_failed_acknowledged"] == 0


def test_prov_coverage_report_makes_no_write(tmp_path):
    """Unlike daily_report, prov_coverage_report appends NOTHING — pure reporting."""
    p = _seed(tmp_path)
    before = p.count()
    prov_coverage_report(p)
    assert p.count() == before


def test_prov_coverage_report_excludes_acknowledged_failures(tmp_path):
    """Mirrors daily_report's OBS-5 exclusion exactly — an acknowledged failure drops out of the
    pass-rate numerator/denominator but is reported separately."""
    p = _seed(tmp_path)
    p.record(activity="commit_merge", item_key="M2", params={"pass": False},
             snapshot_id="snap-2", ts="2026-06-24T10:03:00+00:00")
    acknowledge_verify_failure(p, "snap-2", reason="investigated, root cause fixed",
                              ts="2026-06-24T11:00:00+00:00")
    rep = prov_coverage_report(p)
    assert rep["verify_total"] == 2          # the acked failure is excluded from the denominator
    assert rep["verify_pass_rate"] == 0.5    # unchanged — same as before acknowledgment
    assert rep["verify_failed_acknowledged"] == 1


def test_prov_coverage_report_recent_merges_and_recent_n(tmp_path):
    p = ProvenanceStore(tmp_path)
    for i in range(5):
        p.record(activity="commit_merge", item_key=f"M{i}", before={"v": i}, after={"v": i + 1},
                 snapshot_id=f"snap-{i}", ts=f"2026-06-24T10:0{i}:00+00:00")
    rep = prov_coverage_report(p, recent_n=2)
    assert len(rep["recent_merges"]) == 2
    assert rep["recent_merges"][-1]["item_key"] == "M4"     # append order preserved, most recent last
    assert rep["recent_merges"][-1]["before_sha256"] and rep["recent_merges"][-1]["after_sha256"]


def test_prov_coverage_report_empty_store(tmp_path):
    p = ProvenanceStore(tmp_path)
    rep = prov_coverage_report(p)
    assert rep["total_records"] == 0
    assert rep["by_activity"] == {}
    assert rep["verify_pass_rate"] is None
    assert rep["recent_merges"] == []
