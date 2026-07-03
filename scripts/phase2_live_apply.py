#!/usr/bin/env python3
"""Phase-2 LIVE apply driver — walk the owner-approved auto-accept clusters through the gated merge chain.

The LIVE sibling of ``phase2_apply_reconciled.py`` (the SHADOW driver). For every current deterministic
auto-accept cluster it looks up the owner-approved field-level reconciliation (``reconciled-records-153.json``,
matched by member-key SET) and, in ``--live`` mode, runs the proven per-cluster gated chain:

    snapshot_cluster -> merge_cluster(field_sources, capture POST-PATCH master_version)
                     -> commit_merge(field_sources, expected_master_version)     # re-verifies 11/11 then TRASHES

with, on top of the engine's own computational safety net:

  * REFUSE unless BOTH gates are set (belt-and-suspenders): the engine's out-of-band owner token
    ``ZOT_MERGE_LIVE_ENABLED=I-UNDERSTAND-LIVE-MERGE`` (C-1, never a parameter) AND a driver-local
    ``ZOT_LIVE_APPLY_GATE=I-UNDERSTAND``. Neither -> no live run.
  * ``is_duplicate == "yes"`` asserted per cluster: the human-labeled ``uncertain`` cluster #52
    (members {256MZBTC, ASJD3SKQ}) and any non-"yes" cluster are EXCLUDED from commit and routed to a
    "needs owner confirm" list — the process invariant "auto-trash only confirmed dups" stays true.
  * an append-only JSONL checkpoint (``live-checkpoint.jsonl``) so a 152-cluster run is RESUMABLE across
    interruption: on restart, any cluster whose member-key set is already ``committed`` is skipped.
  * a FIRST-CLUSTER STOP: after the first committed cluster the run HALTS and prints the survivor / trashed
    keys / PROV id + a live re-read, so the owner eyeballs one real merge before authorizing the rest
    (re-invoke with ``--continue-after-first``).
  * FAIL-STOP: any cluster returning ``mode != "committed"`` halts the WHOLE run (never keep trashing after
    an anomaly) and exits non-zero. Per-cluster rollback-on-fail already lives inside ``commit_merge``.
  * per-cluster W3C-PROV (written by the engine) captured into the checkpoint; a quantitative end report.

``--shadow`` (the DEFAULT) runs the exact same per-cluster loop through ``merge.shadow_merge`` — which takes
NO gateway and structurally cannot write — reproducing ``apply-preview.json`` (152/152 verify-clean). The only
writes in shadow mode are local PROV. Live reads hit the Zotero Web API (``dedup_scan`` over a library page).

GATES / ENV:
  --live                       select the live (gated, trashing) run; absence => shadow (safe default)
  --continue-after-first       proceed past the first-cluster stop (monitor the remaining clusters)
  ZOT_MERGE_LIVE_ENABLED       must == "I-UNDERSTAND-LIVE-MERGE" for --live (engine C-1 owner token)
  ZOT_LIVE_APPLY_GATE          must == "I-UNDERSTAND" for --live (driver-local belt-and-suspenders)
  ZOTERO_API_KEY               required (live reads; live writes in --live)
  ZOT_LIBRARY_ID               default 11056739
  RECON_JSON                   path to reconciled-records-153.json
  APPLY_OUT_DIR                default exit-gate-runs/apply  (checkpoint + preview live here)
  ZOT_LIVE_REFRESH_EVERY       refresh the daily_report observability marker every N commits (default 20)
  ZOT_LIVE_INTER_CLUSTER_SLEEP seconds to sleep between commits (default 0; the gateway also honors Backoff)

THIS DRIVER DOES NOT SET ANY TOKEN. The operator sets ``ZOT_MERGE_LIVE_ENABLED`` + ``ZOT_LIVE_APPLY_GATE``
out of band at S2 per docs/runbooks/first-live-mass-merge.md. Build/shadow/test never set them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zotero_write_mcp import merge as M  # noqa: E402
from zotero_write_mcp.merge_live import (  # noqa: E402
    ENABLE_ENV, ENABLE_TOKEN, CommitResult, WebClusterReader, commit_merge, live_merge_enabled,
    merge_cluster,
)
from zotero_write_mcp.observability import daily_report  # noqa: E402
from zotero_write_mcp.dedup import dedup_scan  # noqa: E402
from zotero_write_mcp.client import ZoteroClient  # noqa: E402

# ── constants / env ──────────────────────────────────────────────────────────────
LIBRARY_ID = int(os.environ.get("ZOT_LIBRARY_ID", "11056739"))
BASE = f"http://zotero.org/users/{LIBRARY_ID}/items"
RECON_JSON = os.environ.get(
    "RECON_JSON",
    os.path.join(os.path.dirname(__file__), "..", "exit-gate-runs", "reconciled-records-153.json"))
OUT_DIR = os.environ.get("APPLY_OUT_DIR", os.path.join(os.path.dirname(__file__), "..", "exit-gate-runs", "apply"))
CHECKPOINT_PATH = os.path.join(OUT_DIR, "live-checkpoint.jsonl")
SHADOW_PREVIEW_PATH = os.path.join(OUT_DIR, "live-apply-shadow-preview.json")

APPLY_GATE_ENV = "ZOT_LIVE_APPLY_GATE"
APPLY_GATE_TOKEN = "I-UNDERSTAND"
REFRESH_EVERY = int(os.environ.get("ZOT_LIVE_REFRESH_EVERY", "20"))
INTER_CLUSTER_SLEEP = float(os.environ.get("ZOT_LIVE_INTER_CLUSTER_SLEEP", "0"))

# The one human-labeled "uncertain" apply-set cluster (REV2 PI-1). Belt-and-suspenders alongside the
# is_duplicate!="yes" assertion: even if a reconciliation record were mis-labeled, this member set is
# never committed by this driver.
UNCERTAIN_MEMBERS = frozenset({"256MZBTC", "ASJD3SKQ"})


# ── data structures ────────────────────────────────────────────────────────────────
@dataclass
class ClusterJob:
    """One auto-accept cluster + its approved reconciliation, ready to process."""
    index: int                       # 1-based position in the auto-accept scan order
    master: str
    secondaries: list
    member_keys: list                # sorted, for stable frozenset / logging
    is_duplicate: str                # "yes" | "uncertain" | "UNMATCHED"
    field_sources: dict

    @property
    def members(self) -> frozenset:
        return frozenset(self.member_keys)


@dataclass
class LiveOutcome:
    res: CommitResult
    snapshot_id: Optional[str]


@dataclass
class RunReport:
    mode: str
    counts: Counter
    preview: list = field(default_factory=list)
    needs_confirm: list = field(default_factory=list)
    halted: Optional[str] = None
    stopped_after_first: bool = False
    committed_this_run: int = 0
    total_jobs: int = 0
    checkpoint_path: str = CHECKPOINT_PATH
    wall_clock_s: float = 0.0


# ── library read + reconciliation (verbatim structure from phase2_apply_reconciled.py) ─────
def _data(it):
    return it.get("data", it)


def web_items(client, item_type="-attachment", page=100):
    """Page the whole library via the Web API (honors Retry-After / Backoff)."""
    out, start = [], 0
    while True:
        r = client._client.get(f"{client.web_url}/users/{LIBRARY_ID}/items", headers=client._web_headers,
                               params={"limit": page, "start": start, "itemType": item_type, "format": "json"})
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5"))); continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch); start += len(batch)
        if r.headers.get("Backoff"):
            time.sleep(int(r.headers["Backoff"]))
        if len(batch) < page:
            break
    return out


def load_reconciliation(path=RECON_JSON):
    """{frozenset(member_keys): {"field_sources": {field: source_key}, "is_duplicate": "yes"|...}}."""
    recon = json.load(open(path, encoding="utf-8"))["clusters"]
    fs_by_members = {}
    for c in recon:
        fs = {f: info["source_key"] for f, info in c.get("fields", {}).items()}
        fs_by_members[frozenset(c["member_keys"])] = {
            "field_sources": fs, "is_duplicate": c.get("is_duplicate")}
    return fs_by_members


def build_jobs(auto_clusters, fs_by_members) -> list:
    """Turn dedup_scan auto-accept clusters into ClusterJobs, attaching the approved reconciliation by
    member-key SET. An unmatched cluster gets field_sources={} and is_duplicate="UNMATCHED" (no enrichment,
    never invented sources)."""
    jobs = []
    for i, c in enumerate(auto_clusters, start=1):
        master = c.master_key
        secondaries = [k for k in c.item_keys if k != master]
        rec = fs_by_members.get(frozenset(c.item_keys))
        if rec is None:
            jobs.append(ClusterJob(i, master, secondaries, sorted(c.item_keys), "UNMATCHED", {}))
        else:
            jobs.append(ClusterJob(i, master, secondaries, sorted(c.item_keys),
                                   rec["is_duplicate"] or "UNMATCHED", rec["field_sources"]))
    return jobs


# ── checkpoint (append-only JSONL; resume-skip keys on mode=="committed") ─────────────
def load_committed(checkpoint_path) -> set:
    """The set of member-key frozensets already COMMITTED (skip-on-resume). Only ``mode=="committed"``
    lines cause a skip — a blocked/rolled_back cluster is retried on resume (its PATCH was rolled back)."""
    committed = set()
    if not os.path.exists(checkpoint_path):
        return committed
    with open(checkpoint_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("mode") == "committed":
                committed.add(frozenset(rec.get("member_keys", [])))
    return committed


def append_checkpoint(checkpoint_path, record) -> None:
    """Append one JSON line and fsync — so a crash immediately after a commit still leaves a durable
    'committed' marker (shrinking the resume double-process window to sub-fsync; a re-committed cluster is
    idempotent anyway, and the engine's startup orphan-reconcile covers an in-commit crash)."""
    os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
    with open(checkpoint_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ── the per-cluster legs (injectable for unit tests) ─────────────────────────────────
def live_commit_one(job: ClusterJob, reader, gateway, prov, *, library_id, now) -> LiveOutcome:
    """The proven live gated chain for ONE cluster (mirrors phase2_commit_merge_live.py's ordering):
    snapshot -> merge_cluster(field_sources) [capture POST-PATCH master version] -> commit_merge(field_sources,
    expected_master_version). A drift at the PATCH phase yields a synthetic ``blocked`` result so the driver
    fail-stops (re-snapshot required — never merge stale data)."""
    snap = M.snapshot_cluster(reader, job.master, job.secondaries, prov=prov)
    plan = merge_cluster(snap, reader, gateway, library_id=library_id, field_sources=job.field_sources)
    if plan.drifted:
        return LiveOutcome(
            CommitResult(mode="blocked", verify_passed=None,
                         reason=f"merge_cluster drift on {plan.drift_keys} — re-snapshot required"),
            snap.snapshot_id)
    res = commit_merge(snap, reader, gateway, prov, library_id=library_id, now=now,
                       field_sources=job.field_sources, expected_master_version=plan.master_version)
    return LiveOutcome(res, snap.snapshot_id)


def shadow_one(job: ClusterJob, reader, prov, *, by_key=None) -> dict:
    """SHADOW leg (no gateway, structurally cannot write) — reproduces phase2_apply_reconciled.py's preview
    record: verify verdict + the survivor changes the merge would make."""
    by_key = by_key or {}
    try:
        sr = M.shadow_merge(reader, job.master, job.secondaries, prov=prov,
                            field_sources=job.field_sources, library_base=BASE)
        ok = bool(sr.passed)
        failed = [ch.name for ch in sr.integrity.failed]
        pm = sr.projection.items[job.master].fields
        sm = by_key.get(job.master, {})
        changes = {f: {"from": sm.get(f), "to": pm.get(f)}
                   for f in set(list(job.field_sources) + ["extra"]) if pm.get(f) != sm.get(f)}
    except Exception as e:  # noqa: BLE001 — a projection error must not abort the whole preview
        ok, failed, changes = False, [f"EXCEPTION: {type(e).__name__}: {e}"], {}
    return {"index": job.index, "master": job.master, "trash": job.secondaries,
            "is_duplicate": job.is_duplicate, "verify_pass": ok, "failed_checks": failed,
            "survivor_changes": changes, "title": by_key.get(job.master, {}).get("title")}


def _reread_confirm(reader, master, trashed) -> Optional[dict]:
    """Best-effort live re-read for the first-cluster stop: secondary trashed(deleted:1) AND present;
    master carries dc:replaces->secondary. Never raises (a re-read hiccup must not crash the run)."""
    try:
        out = {"secondaries": {}}
        master_live = _data(reader.get_item(master))
        dc = master_live.get("relations", {}).get("dc:replaces", [])
        dc = [dc] if isinstance(dc, str) else dc
        for k in trashed:
            sec = _data(reader.get_item(k))
            out["secondaries"][k] = {
                "trashed_not_purged": sec.get("deleted") in (1, "1", True),
                "present": True,
                "master_dc_replaces": any(M._rel_target_key(v) == k for v in dc)}
        return out
    except Exception as e:  # noqa: BLE001
        return {"reread_error": f"{type(e).__name__}: {e}"}


# ── the orchestration loop ───────────────────────────────────────────────────────────
def run(jobs, reader, gateway, prov, *, mode, library_id, checkpoint_path,
        continue_after_first=False, by_key=None, commit_one=live_commit_one, shadow_fn=shadow_one,
        clock: Optional[Callable[[], datetime]] = None, refresh_every=REFRESH_EVERY,
        inter_sleep=INTER_CLUSTER_SLEEP, emit=print) -> RunReport:
    """Walk `jobs`. SHADOW: preview every cluster (incl. the excluded/uncertain, labeled) to reproduce
    apply-preview.json. LIVE: skip already-committed (resume), EXCLUDE non-"yes" (route to needs-confirm),
    else commit; fail-STOP on any mode!=committed; STOP after the first committed cluster unless
    --continue-after-first. Injectable commit_one/shadow_fn/clock make the live path unit-testable offline."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    counts: Counter = Counter()
    report = RunReport(mode=mode, counts=counts, total_jobs=len(jobs), checkpoint_path=checkpoint_path)
    t0 = time.monotonic()

    already = load_committed(checkpoint_path) if mode == "live" else set()
    if already:
        emit(f"[RESUME]   {len(already)} cluster(s) already committed in {os.path.basename(checkpoint_path)} — will skip")

    if mode == "live":
        # Prime observability (C-2 freshness) before the first commit; refreshed on a cadence below.
        daily_report(prov, ts=clock().isoformat())

    for job in jobs:
        members = job.members
        excluded = (job.is_duplicate != "yes") or (members == UNCERTAIN_MEMBERS)

        # ---- SHADOW: preview ALL (incl. excluded) so the artifact matches apply-preview.json ----
        if mode == "shadow":
            rec = shadow_fn(job, reader, prov, by_key=by_key)
            report.preview.append(rec)
            counts["shadow_pass" if rec["verify_pass"] else "shadow_fail"] += 1
            if excluded:
                report.needs_confirm.append(
                    {"index": job.index, "members": sorted(members), "is_duplicate": job.is_duplicate})
            continue

        # ---- LIVE ----
        if members in already:
            counts["skipped_resumed"] += 1
            emit(f"[SKIP]     cluster {job.index}: already committed (checkpoint) {sorted(members)}")
            continue

        if excluded:
            counts["excluded"] += 1
            report.needs_confirm.append(
                {"index": job.index, "members": sorted(members), "is_duplicate": job.is_duplicate})
            emit(f"[EXCLUDE]  cluster {job.index}: is_duplicate={job.is_duplicate!r} -> NEEDS OWNER CONFIRM, "
                 f"NOT committed {sorted(members)}")
            continue

        now = clock()
        # Fail-stop must cover EXCEPTIONS too, not just mode!=committed: a raw GatewayError / network drop
        # can escape snapshot_cluster/merge_cluster (only ConcurrencyConflictError is caught there). Convert
        # it into a clean halt + audit line + non-zero exit rather than an uncaught traceback that skips the
        # report. The merge_cluster PATCH phase is reversible (rollback_merge state b) and a re-run
        # re-snapshots idempotently, so halting-and-preserving is safe.
        try:
            outcome = commit_one(job, reader, gateway, prov, library_id=library_id, now=now)
        except Exception as e:  # noqa: BLE001
            append_checkpoint(checkpoint_path, {
                "member_keys": sorted(members), "master": job.master, "snapshot_id": None,
                "mode": "error", "trashed": [], "prov_id": None, "verify_passed": False,
                "reason": f"{type(e).__name__}: {e}", "ts": now.isoformat(), "index": job.index})
            counts["error"] += 1
            report.halted = f"cluster {job.index} raised {type(e).__name__}: {e}"
            emit("\n" + "!" * 78)
            emit(f"[FAIL-STOP] {report.halted}")
            emit(f"[FAIL-STOP] a merge_cluster PATCH may be uncommitted-but-reversible; investigate + "
                 f"rollback/re-run before resuming (checkpoint: {checkpoint_path})")
            emit("!" * 78)
            break
        res = outcome.res
        append_checkpoint(checkpoint_path, {
            "member_keys": sorted(members), "master": job.master, "snapshot_id": outcome.snapshot_id,
            "mode": res.mode, "trashed": list(res.trashed or []), "prov_id": res.intent_prov_id,
            "verify_passed": res.verify_passed, "reason": res.reason,
            "ts": now.isoformat(), "index": job.index})
        counts[res.mode] += 1

        # ---- FAIL-STOP: any non-committed outcome halts the WHOLE run (do not keep trashing) ----
        if res.mode != "committed":
            report.halted = f"cluster {job.index} returned mode={res.mode!r}: {res.reason}"
            emit("\n" + "!" * 78)
            emit(f"[FAIL-STOP] {report.halted}")
            if res.rollback is not None:
                emit(f"[FAIL-STOP] rollback: state={res.rollback.state} ok={res.rollback.ok} "
                     f"failures={res.rollback.failures}")
            emit(f"[FAIL-STOP] trashed-before-halt={list(res.trashed or [])}  "
                 f"(checkpoint preserves progress: {checkpoint_path})")
            emit("!" * 78)
            break

        report.committed_this_run += 1
        counts["secondaries_trashed"] += len(res.trashed or [])
        emit(f"[COMMIT]   cluster {job.index} master={job.master} trashed={list(res.trashed or [])} "
             f"prov={res.intent_prov_id}")

        # ---- FIRST-CLUSTER STOP: prove one real merge, then require --continue-after-first ----
        if not continue_after_first:
            report.stopped_after_first = True
            confirm = _reread_confirm(reader, job.master, list(res.trashed or []))
            emit("\n" + "=" * 78)
            emit("[FIRST-STOP] one cluster committed — HALTING for owner inspection.")
            emit(f"[FIRST-STOP] survivor(master) : {job.master}")
            emit(f"[FIRST-STOP] trashed(dups)    : {list(res.trashed or [])}")
            emit(f"[FIRST-STOP] snapshot_id/prov : {outcome.snapshot_id} / {res.intent_prov_id}")
            emit(f"[FIRST-STOP] live re-read      : {json.dumps(confirm, ensure_ascii=False)}")
            emit("[FIRST-STOP] inspect the survivor + trash in Zotero, then re-invoke with "
                 "--continue-after-first to process the rest.")
            emit("=" * 78)
            break

        # ---- observability refresh cadence + polite inter-cluster spacing ----
        if refresh_every and report.committed_this_run % refresh_every == 0:
            daily_report(prov, ts=clock().isoformat())
        if inter_sleep:
            time.sleep(inter_sleep)

    report.wall_clock_s = round(time.monotonic() - t0, 2)
    return report


# ── shadow artifact + report printing ────────────────────────────────────────────────
def _write_shadow_artifact(report: RunReport):
    os.makedirs(OUT_DIR, exist_ok=True)
    failures = [(p["master"], p["failed_checks"]) for p in report.preview if not p["verify_pass"]]
    unmatched = [p["master"] for p in report.preview if p["is_duplicate"] == "UNMATCHED"]
    json.dump({"library_id": LIBRARY_ID, "auto_accept": len(report.preview),
               "verify_passed": len(report.preview) - len(failures), "failures": failures,
               "unmatched": unmatched, "clusters": report.preview},
              open(SHADOW_PREVIEW_PATH, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return failures, unmatched


def _print_report(report: RunReport, emit=print) -> int:
    c = report.counts
    emit("\n" + "=" * 78)
    if report.mode == "shadow":
        failures, unmatched = _write_shadow_artifact(report)
        n = len(report.preview)
        enriched = sum(1 for p in report.preview if any(f != "extra" for f in p["survivor_changes"]))
        aliased = sum(1 for p in report.preview if "extra" in p["survivor_changes"])
        emit(f"SHADOW APPLY (zero writes)  ·  {n} auto-accept clusters")
        emit(f"  verify pass          : {n - len(failures)}/{n}")
        emit(f"  with field enrichment: {enriched}    with citekey alias: {aliased}")
        emit(f"  unmatched (no recon) : {len(unmatched)}")
        emit(f"  needs owner confirm  : {len(report.needs_confirm)}  {report.needs_confirm or ''}")
        emit(f"  shadow failures      : {len(failures)}  {failures[:5] if failures else ''}")
        emit(f"  preview written      : {SHADOW_PREVIEW_PATH}")
        gate = not failures
        emit(f"  RESULT               : {'ALL CLUSTERS PROJECT+VERIFY CLEAN' if gate else 'FAILURES — review'}")
        emit("=" * 78)
        return 0 if gate else 2

    # live
    emit(f"LIVE APPLY  ·  {report.total_jobs} auto-accept clusters  ·  {report.wall_clock_s}s")
    emit(f"  committed            : {c['committed']}   (secondaries trashed: {c['secondaries_trashed']})")
    emit(f"  shadow (token off)   : {c['shadow']}")
    emit(f"  blocked              : {c['blocked']}")
    emit(f"  rolled_back          : {c['rolled_back'] + c['rollback_failed']}")
    emit(f"  excluded (not 'yes') : {c['excluded']}  {report.needs_confirm or ''}")
    emit(f"  skipped (resumed)    : {c['skipped_resumed']}")
    emit(f"  checkpoint           : {report.checkpoint_path}")
    if report.halted:
        emit(f"  HALTED               : {report.halted}")
        emit("=" * 78)
        return 3
    if report.stopped_after_first:
        emit("  STOPPED after first committed cluster — re-invoke with --continue-after-first.")
        emit("=" * 78)
        return 0
    emit("  RESULT               : run complete.")
    emit("=" * 78)
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────────────
def _check_live_gates() -> bool:
    """LIVE requires BOTH: the engine's out-of-band owner token AND the driver-local gate."""
    return live_merge_enabled() and os.environ.get(APPLY_GATE_ENV) == APPLY_GATE_TOKEN


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase-2 live apply driver (shadow by default).")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--live", action="store_true",
                     help="perform the gated LIVE run (requires BOTH env gates); absence => shadow.")
    grp.add_argument("--shadow", action="store_true",
                     help="explicit shadow (no writes) — the default; makes intent self-documenting.")
    ap.add_argument("--continue-after-first", action="store_true",
                    help="proceed past the first-cluster stop and process the remaining clusters.")
    args = ap.parse_args(argv)
    mode = "live" if args.live else "shadow"

    # GATE checks FIRST (before any network) so a refusal is instant and side-effect-free.
    if mode == "live":
        if not _check_live_gates():
            print(f"REFUSE: --live requires BOTH {ENABLE_ENV}={ENABLE_TOKEN} AND "
                  f"{APPLY_GATE_ENV}={APPLY_GATE_TOKEN}. Neither/one set -> no live run. "
                  "(Set them out of band per docs/runbooks/first-live-mass-merge.md; the driver never sets them.)")
            return 1
    else:
        if live_merge_enabled():
            print(f"REFUSE: shadow mode but {ENABLE_ENV} is set — unset it "
                  "(shadow must never run with the live token active).")
            return 1
    if not os.environ.get("ZOTERO_API_KEY"):
        print("FAIL: ZOTERO_API_KEY not set (needed for the live library read).")
        return 1

    # Load reconciliation + scan the live library.
    fs_by_members = load_reconciliation(RECON_JSON)
    print(f"[RECON]    loaded {len(fs_by_members)} approved reconciliations from {os.path.basename(RECON_JSON)}")

    client = ZoteroClient(); client._library_id = LIBRARY_ID
    reader = WebClusterReader(client, LIBRARY_ID)
    prov = client.prov

    print(f"[SCAN]     reading library {LIBRARY_ID} + dedup_scan ...", flush=True)
    items = web_items(client)
    by_key = {(_data(it).get("key") or it.get("key")): _data(it) for it in items}
    rep = dedup_scan(items)
    auto = [cl for cl in rep["candidate_clusters"] if cl.auto_accept]
    jobs = build_jobs(auto, fs_by_members)
    print(f"[SCAN]     {len(items)} items; {len(auto)} auto-accept clusters; "
          f"{sum(1 for j in jobs if j.is_duplicate == 'yes')} confirmed / "
          f"{sum(1 for j in jobs if j.is_duplicate != 'yes')} excluded", flush=True)

    report = run(jobs, reader, gateway=client.gateway, prov=prov, mode=mode, library_id=LIBRARY_ID,
                 checkpoint_path=CHECKPOINT_PATH, continue_after_first=args.continue_after_first,
                 by_key=by_key)
    return _print_report(report)


if __name__ == "__main__":
    sys.exit(main())
