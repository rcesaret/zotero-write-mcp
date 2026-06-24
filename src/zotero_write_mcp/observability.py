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


def _has_pass(r: dict) -> bool:
    p = r.get("params")
    return isinstance(p, dict) and "pass" in p


def daily_report(prov: ProvenanceStore, *, sample_size: int = 10, ts: Optional[str] = None,
                 pass_rate_floor: float = 1.0) -> dict:
    """Compute the Phase-2 gate metrics from PROV and append a `daily_report` marker (the freshness +
    HEALTH artifact). The ONLY write is the marker append (no Zotero mutation).

    `status` is **"degraded"** when the recent verify-pass-rate is below `pass_rate_floor` (default 1.0 —
    the Phase-2 exit gate demands 100% verify-pass), else "ok". `observability_is_fresh` rejects a non-ok
    marker, so a report that RUNS but records bad health blocks live commits (F3/C-2: the gate must detect a
    SICK library, not only a DEAD report job). A `None` rate (no recent verifies) is "ok" — the per-commit
    verify still gates each merge.
    """
    recs = prov.all_records()
    verifies = [r for r in recs if r.get("activity") in VERIFY_ACTIVITIES and _has_pass(r)]
    passed = [r for r in verifies if r["params"]["pass"] is True]
    commits = [r for r in recs if r.get("activity") == "commit_merge"]
    rate = (len(passed) / len(verifies)) if verifies else None
    status = "degraded" if (rate is not None and rate < pass_rate_floor) else "ok"

    metrics = {
        "verify_pass_rate": rate,
        "verify_total": len(verifies),
        "verify_passed": len(passed),
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
