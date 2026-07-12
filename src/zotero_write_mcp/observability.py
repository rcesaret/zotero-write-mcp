"""Observability — query_provenance + daily report + freshness guard (Phase 2; read-only over PROV).

Built FIRST in Phase 2 (Stage-E H-5: observability must be live BEFORE any live merge commit). Every
function is READ-ONLY over the ProvenanceStore except `daily_report`, which appends a single
`daily_report` marker — there is NO Zotero write path here. That marker (status + ts) is the
machine-checkable freshness artifact that `commit_merge`'s runtime gate (critique C-2/H-4) reads via
`observability_is_fresh()`: stale/absent observability fails the live commit closed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from zotero_write_mcp.provenance import ProvenanceStore


def _parse_ts(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _history_entry(r: dict) -> dict:
    e = r.get("entity", {}) or {}
    return {
        "prov_id": r.get("prov_id"),
        "ts": r.get("ts"),
        "activity": r.get("activity"),
        "agent": r.get("agent"),
        "tool_version": r.get("tool_version"),
        "params": r.get("params"),
        "item_key": e.get("item_key"),
        "before_sha256": e.get("before_sha256"),
        "after_sha256": e.get("after_sha256"),
        # reversibility index — ADR-008: "the audit trail IS the rollback index"
        "reversibility": {
            "snapshot_id": r.get("was_derived_from"),
            "before_blob": e.get("before_blob"),
            "after_blob": e.get("after_blob"),
            "reverse": r.get("reverse"),
        },
    }


def query_provenance(prov: ProvenanceStore, item_key: str) -> list:
    """TC-11: the full PROV history for an item, in append order, each entry carrying its reversibility
    index (snapshot_id / before+after blobs / reverse op). Read-only."""
    return [_history_entry(r) for r in prov.query(item_key)]


# Activities that carry a real verify verdict (`params.pass`). The verify-pass-rate is computed ONLY over
# these (OBS-4): "any record with a pass key" would silently pool unrelated activities and could be inflated
# by a future activity that happens to set params.pass.
VERIFY_ACTIVITIES = frozenset({"shadow_merge", "commit_merge", "commit_merge_shadow", "commit_merge_verify"})

# OBS-5 (S2 field finding, 2026-07-07; owner-approved): a human-reviewed verify failure is acknowledged by
# appending this record (was_derived_from = the failed verify's snapshot_id). Because PROV is append-only,
# a single caught failure would otherwise hold the all-time pass-rate below the 1.0 floor FOREVER and
# deadlock every future live commit — the C-2 gate could never recover even after the incident was
# investigated and fixed. Acknowledged failures are excluded from the health rate and reported separately
# in `verify_failed_acknowledged` (visible, never hidden); UNacknowledged failures still degrade the report
# and block live commits, fail-closed. The per-cluster 11-check verify gate is unaffected by this.
ACK_ACTIVITY = "verify_failure_acknowledged"


def _acknowledged_snapshot_ids(recs: list) -> set:
    return {r.get("was_derived_from") for r in recs
            if r.get("activity") == ACK_ACTIVITY and r.get("was_derived_from")}


def acknowledge_verify_failure(prov: ProvenanceStore, snapshot_id: str, *, reason: str,
                               acknowledged_by: str = "owner", ts: Optional[str] = None) -> dict:
    """OBS-5: append the human-review acknowledgment for ONE failed verify, matched by snapshot_id.
    Fail-closed validation: refuses (ValueError) unless a FAILED verify record with that snapshot_id
    exists, refuses an empty reason, and is idempotent (a second acknowledgment of the same snapshot_id
    is refused rather than double-recorded). The failed record itself is never altered (append-only)."""
    if not (reason or "").strip():
        raise ValueError("acknowledge_verify_failure: a non-empty reason is required")
    recs = prov.all_records()
    failed = [r for r in recs if r.get("activity") in VERIFY_ACTIVITIES and _has_pass(r)
              and r["params"].get("pass") is False and r.get("was_derived_from") == snapshot_id]
    if not failed:
        raise ValueError(f"acknowledge_verify_failure: no FAILED verify record with "
                         f"snapshot_id {snapshot_id!r} — nothing to acknowledge")
    if snapshot_id in _acknowledged_snapshot_ids(recs):
        raise ValueError(f"acknowledge_verify_failure: snapshot_id {snapshot_id!r} is already acknowledged")
    return prov.record(
        activity=ACK_ACTIVITY, agent=acknowledged_by, snapshot_id=snapshot_id, ts=ts,
        params={"reason": reason, "acknowledged_by": acknowledged_by,
                "failed_checks": (failed[0].get("params") or {}).get("failed")})


def _has_pass(r: dict) -> bool:
    p = r.get("params")
    return isinstance(p, dict) and "pass" in p


def daily_report(prov: ProvenanceStore, *, sample_size: int = 10, ts: Optional[str] = None,
                 pass_rate_floor: float = 1.0) -> dict:
    """Compute the Phase-2 gate metrics from PROV and append a `daily_report` marker (the freshness +
    HEALTH artifact). The ONLY write is the marker append (no Zotero mutation).

    `status` is **"degraded"** when the verify-pass-rate is below `pass_rate_floor` (default 1.0 —
    the Phase-2 exit gate demands 100% verify-pass), else "ok". `observability_is_fresh` rejects a non-ok
    marker, so a report that RUNS but records bad health blocks live commits (F3/C-2: the gate must detect a
    SICK library, not only a DEAD report job). A `None` rate (no verifies) is "ok" — the per-commit
    verify still gates each merge.

    OBS-5: the rate is computed over all verify records MINUS failures a human has explicitly acknowledged
    (`verify_failure_acknowledged`, matched by snapshot_id) — otherwise one caught-and-rolled-back failure
    would degrade the all-time rate forever and permanently deadlock the C-2 live-commit gate. Acknowledged
    failures stay in the audit and are reported here in `verify_failed_acknowledged`; an UNacknowledged
    failure still degrades the report, fail-closed.
    """
    recs = prov.all_records()
    all_verifies = [r for r in recs if r.get("activity") in VERIFY_ACTIVITIES and _has_pass(r)]
    acked_ids = _acknowledged_snapshot_ids(recs)
    acked_failures = [r for r in all_verifies
                      if r["params"].get("pass") is False and r.get("was_derived_from") in acked_ids]
    _acked = {id(r) for r in acked_failures}
    verifies = [r for r in all_verifies if id(r) not in _acked]
    passed = [r for r in verifies if r["params"]["pass"] is True]
    commits = [r for r in recs if r.get("activity") == "commit_merge"]
    rate = (len(passed) / len(verifies)) if verifies else None
    status = "degraded" if (rate is not None and rate < pass_rate_floor) else "ok"

    metrics = {
        "verify_pass_rate": rate,
        "verify_total": len(verifies),
        "verify_passed": len(passed),
        "verify_failed_acknowledged": len(acked_failures),
        "merges_committed": len(commits),
        "prov_records": len(recs),
        "pass_rate_floor": pass_rate_floor,
        "status": status,
    }
    sampled_audit = [{
        "item_key": (r.get("entity") or {}).get("item_key"),
        "snapshot_id": r.get("was_derived_from"),
        "before_blob": (r.get("entity") or {}).get("before_blob"),
        "after_blob": (r.get("entity") or {}).get("after_blob"),
    } for r in commits[:sample_size]]

    rec = prov.record(activity="daily_report", agent="observability", params=metrics, ts=ts)
    return {**metrics, "ts": rec["ts"], "sampled_audit": sampled_audit}


def prov_coverage_report(prov: ProvenanceStore, *, recent_n: int = 20) -> dict:
    """S5a F2: the owner-facing "did every mutation get audited?" answer (ADR-008 interlock — the
    PROV store IS the rollback index, so its coverage is only trustworthy if it's visible). Read-only
    aggregation over the WHOLE append-only log: total record count, a per-activity breakdown, the same
    verify-pass-rate computation `daily_report` uses (reusing `VERIFY_ACTIVITIES` / the OBS-5
    acknowledge exclusion, so the two never drift apart), and the last `recent_n` merges with their
    before/after sha256 for a spot-check. Makes NO write of any kind (not even a marker append) —
    unlike `daily_report`, this is pure reporting."""
    recs = prov.all_records()
    by_activity: dict = {}
    for r in recs:
        a = r.get("activity") or "?"
        by_activity[a] = by_activity.get(a, 0) + 1

    all_verifies = [r for r in recs if r.get("activity") in VERIFY_ACTIVITIES and _has_pass(r)]
    acked_ids = _acknowledged_snapshot_ids(recs)
    acked_failures = [r for r in all_verifies
                      if r["params"].get("pass") is False and r.get("was_derived_from") in acked_ids]
    _acked = {id(r) for r in acked_failures}
    verifies = [r for r in all_verifies if id(r) not in _acked]
    passed = [r for r in verifies if r["params"]["pass"] is True]

    commits = [r for r in recs if r.get("activity") == "commit_merge"]
    recent_merges = [{
        "item_key": (r.get("entity") or {}).get("item_key"),
        "ts": r.get("ts"),
        "snapshot_id": r.get("was_derived_from"),
        "before_sha256": (r.get("entity") or {}).get("before_sha256"),
        "after_sha256": (r.get("entity") or {}).get("after_sha256"),
    } for r in commits[-recent_n:]]

    return {
        "total_records": len(recs),
        "by_activity": by_activity,
        "verify_total": len(verifies),
        "verify_passed": len(passed),
        "verify_pass_rate": (len(passed) / len(verifies)) if verifies else None,
        "verify_failed_acknowledged": len(acked_failures),
        "merges_committed": len(commits),
        "recent_merges": recent_merges,
    }


def latest_daily_report(prov: ProvenanceStore) -> Optional[dict]:
    reps = [r for r in prov.all_records() if r.get("activity") == "daily_report"]
    return reps[-1] if reps else None


def observability_is_fresh(prov: ProvenanceStore, *, window_seconds: float, now: datetime) -> bool:
    """Critique C-2/H-4 RUNTIME guard: True iff a `daily_report` marker with `status=="ok"` exists and its
    ts is within `window_seconds` of `now`. `commit_merge` calls this BEFORE any trash — stale/absent/non-ok
    observability fails the live commit closed (a one-time release check is not enough; the report job could
    die silently after enable)."""
    rep = latest_daily_report(prov)
    if rep is None or (rep.get("params") or {}).get("status") != "ok":
        return False
    age = (now - _parse_ts(rep["ts"])).total_seconds()
    return 0 <= age <= window_seconds
