"""OBS-5 — acknowledged verify failures (S2 field finding, 2026-07-07).

A single caught-and-rolled-back verify failure lives forever in the append-only PROV store; before
OBS-5 it held the all-time pass-rate below the 1.0 floor permanently, deadlocking the C-2 live-commit
gate with no recovery path. These tests pin the acknowledgment semantics: an UNacknowledged failure
still degrades (fail-closed); an acknowledged one is excluded from the health rate but stays in the
audit and is reported in `verify_failed_acknowledged`; acknowledgment is validated and idempotent.
"""
from datetime import datetime, timezone

import pytest

from zotero_write_mcp.observability import (
    acknowledge_verify_failure, daily_report, observability_is_fresh,
)
from zotero_write_mcp.provenance import ProvenanceStore

SNAP = "snap-fail-1"


def _store_with_failure(tmp_path):
    """One passed verify + one FAILED verify (carrying a snapshot_id, like commit_merge_verify)."""
    p = ProvenanceStore(tmp_path)
    p.record(activity="shadow_merge", item_key="A", params={"pass": True},
             ts="2026-07-07T10:00:00+00:00")
    p.record(activity="commit_merge_verify", item_key="M", snapshot_id=SNAP,
             params={"pass": False, "failed": ["citekey-preservation"]},
             ts="2026-07-07T10:01:00+00:00")
    return p


def test_unacknowledged_failure_degrades_and_blocks(tmp_path):
    """Fail-closed baseline: no acknowledgment -> degraded -> not fresh (C-2 blocks live commits)."""
    p = _store_with_failure(tmp_path)
    rep = daily_report(p, ts="2026-07-07T12:00:00+00:00")
    assert rep["status"] == "degraded" and rep["verify_pass_rate"] == 0.5
    assert rep["verify_failed_acknowledged"] == 0
    now = datetime(2026, 7, 7, 12, 1, tzinfo=timezone.utc)
    assert not observability_is_fresh(p, window_seconds=48 * 3600, now=now)


def test_acknowledged_failure_is_excluded_and_reported(tmp_path):
    """The reviewed failure no longer degrades; it is counted visibly, not hidden."""
    p = _store_with_failure(tmp_path)
    acknowledge_verify_failure(p, SNAP, reason="recon data defect; fixed in recon v2",
                               ts="2026-07-07T11:00:00+00:00")
    rep = daily_report(p, ts="2026-07-07T12:00:00+00:00")
    assert rep["status"] == "ok" and rep["verify_pass_rate"] == 1.0
    assert rep["verify_total"] == 1 and rep["verify_passed"] == 1
    assert rep["verify_failed_acknowledged"] == 1
    now = datetime(2026, 7, 7, 12, 1, tzinfo=timezone.utc)
    assert observability_is_fresh(p, window_seconds=48 * 3600, now=now)


def test_acknowledgment_does_not_cover_new_failures(tmp_path):
    """A NEW failure (different snapshot_id) after an acknowledgment degrades again — the gate
    stays fail-closed for anything a human has not reviewed."""
    p = _store_with_failure(tmp_path)
    acknowledge_verify_failure(p, SNAP, reason="reviewed", ts="2026-07-07T11:00:00+00:00")
    p.record(activity="commit_merge_verify", item_key="M2", snapshot_id="snap-fail-2",
             params={"pass": False, "failed": ["collections"]}, ts="2026-07-07T11:30:00+00:00")
    rep = daily_report(p, ts="2026-07-07T12:00:00+00:00")
    assert rep["status"] == "degraded"
    assert rep["verify_failed_acknowledged"] == 1


def test_acknowledge_refuses_unknown_snapshot(tmp_path):
    p = _store_with_failure(tmp_path)
    with pytest.raises(ValueError, match="no FAILED verify record"):
        acknowledge_verify_failure(p, "no-such-snapshot", reason="x")
    assert daily_report(p, ts="2026-07-07T12:00:00+00:00")["status"] == "degraded"


def test_acknowledge_refuses_passed_verify_snapshot(tmp_path):
    """A snapshot_id that only matches a PASSED verify is not acknowledgeable."""
    p = ProvenanceStore(tmp_path)
    p.record(activity="commit_merge_verify", item_key="A", snapshot_id="snap-ok",
             params={"pass": True}, ts="2026-07-07T10:00:00+00:00")
    with pytest.raises(ValueError, match="no FAILED verify record"):
        acknowledge_verify_failure(p, "snap-ok", reason="x")


def test_acknowledge_refuses_empty_reason_and_double_ack(tmp_path):
    p = _store_with_failure(tmp_path)
    with pytest.raises(ValueError, match="reason"):
        acknowledge_verify_failure(p, SNAP, reason="   ")
    acknowledge_verify_failure(p, SNAP, reason="reviewed", ts="2026-07-07T11:00:00+00:00")
    with pytest.raises(ValueError, match="already acknowledged"):
        acknowledge_verify_failure(p, SNAP, reason="reviewed again")


def test_ack_record_carries_audit_fields(tmp_path):
    p = _store_with_failure(tmp_path)
    acknowledge_verify_failure(p, SNAP, reason="recon v2 fix", acknowledged_by="owner",
                               ts="2026-07-07T11:00:00+00:00")
    acks = [r for r in p.all_records() if r.get("activity") == "verify_failure_acknowledged"]
    assert len(acks) == 1
    a = acks[0]
    assert a.get("was_derived_from") == SNAP
    assert a["params"]["reason"] == "recon v2 fix"
    assert a["params"]["acknowledged_by"] == "owner"
    assert a["params"]["failed_checks"] == ["citekey-preservation"]
