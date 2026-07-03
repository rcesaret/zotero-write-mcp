# Runbook — First Live Mass Merge (S2)

**Audience:** Rudolf Cesaretti (owner/operator), executing the first *irreversible* mass merge of the live
Zotero library (~9,088 items, library `11056739`). This runbook **is** the S2 execution script — S2 is a
runbook execution, not a coding sprint. Follow it top to bottom; do not skip the drill.

**What this does.** Walks the **152 owner-approved deterministic auto-accept dedup clusters** through the
gated merge chain (`snapshot → merge_cluster → verify 11/11 → commit_merge → trash-not-purge`), one cluster
at a time, with a first-cluster stop, fail-stop, per-cluster PROV, and a resumable checkpoint. Of the 152,
**151 are committed**; the one human-labeled `uncertain` cluster (**`256MZBTC/ASJD3SKQ`**) is **excluded**
and routed to "needs owner confirm" — it is **not** merged here.

**Reversibility.** Every merge is reversible *only* through the built path: secondaries are **trashed, never
purged**, and `rollback_merge` un-trashes + reverts the master + re-parents children. There is no other
undo. Treat "trash is recoverable" as a precondition you *verify*, not assume (Pre-flight §c, Drill).

**Driver:** [`scripts/phase2_live_apply.py`](../../scripts/phase2_live_apply.py) — the LIVE sibling of the
shadow driver `phase2_apply_reconciled.py`. It **never sets any token**; you set the gates out of band
(Go §1). It refuses to run live unless **both** gates are set.

> Commands below are **PowerShell** (Windows 11). Run from the engine repo root `C:\Users\rcesa\zotero-write-mcp`.
> `ZOTERO_API_KEY` must be present in the environment (as for every live-read script).

---

## 0. Preconditions (must all be true before you start)

- [ ] **S0 shipped and verify check #11 is live-effective.** `WebClusterReader.get_citekey`
      (`src/zotero_write_mcp/merge_live.py`) parses the pinned `Citation Key:` line from `extra`, else the
      `citationKey` field — it does **not** `return None`. (Confirmed in the harness build-log, Session 16 /
      S0: `citationKey` present on 100 % of items by web-API survey, so #11 genuinely bites.) If it ever
      returns `None`, **STOP** — #11 would be inert and the merge unsafe.
- [ ] **Engine suite green** at the S1 head: `uv run --with pytest python -m pytest` → **301 passed / 1
      xfailed** (the 1 xfail = the documented master-version-drift residual).
- [ ] **Shadow run is 152/152 verify-clean:** `uv run python scripts/phase2_live_apply.py --shadow` →
      `152/152`, `0 failures`, `1 needs owner confirm` (the uncertain pair). This is a **read-only** dry run;
      run it now to confirm the current library state still projects clean.

---

## 1. Pre-flight

### a. Redeploy the engine to the new S1 head
The running MCP server holds the installed entrypoint, so redeploy from a terminal with **no live Claude
Code / MCP session attached** (REC5 c.1). This picks up the S1 driver + the S0 honesty fixes.

```powershell
# Close any Claude Code / MCP session using the engine first, then:
uv tool install C:\Users\rcesa\zotero-write-mcp --force
```

> The S1 *driver* runs as a standalone script (`uv run python scripts/…`), so it does not strictly need the
> MCP redeploy; but redeploy anyway so the deployed server and the live code agree before you touch the library.

### b. Confirm S0 / check #11 (see §0) — do not proceed on a fake-green gate.

### c. Zotero backup + verify trash is recoverable (the real restore path)
1. In Zotero desktop: **File → Export Library…** → export a full backup (Zotero RDF or the format you keep).
2. A copy of the live `zotero.sqlite` is a **read backstop only** — it is **not** a restore path (never write
   it back). The real restore path is `rollback_merge` + Zotero **Trash** recovery.
3. **Verify trash recovery works** before trusting it: in Zotero, right-click any item → *Move Item to
   Trash*, then open **Trash**, right-click → *Restore to Library*. Confirm it returns. (The mandatory drill
   in §2 proves the programmatic path end-to-end.)

### d. Owner-confirm the two carve-outs
- [ ] **Uncertain cluster `256MZBTC/ASJD3SKQ`** — the driver **excludes** it (routes to "needs owner
      confirm"). Confirm you accept it is NOT merged in this run. (Resolve it separately, later.)
- [ ] **Residual reconciliation #53** — a `yes` record in `reconciled-records-153.json` that the current
      `dedup_scan` no longer auto-accepts; it never enters the loop. Confirm no action expected here.

---

## 2. Rollback drill — MANDATORY, on throwaways (do not skip)

Prove the full gated chain **and its rollback** on throwaway items, net-zero, before touching real data.
This is the single most important gate between you and an irreversible mistake.

```powershell
$env:ZOT_PHASE2_LIVE_GATE = "I-UNDERSTAND"
uv run python scripts/phase2_commit_merge_live.py
Remove-Item Env:\ZOT_PHASE2_LIVE_GATE
```

**Pass condition (must observe):** the script prints
`PASS: live snapshot → merge → COMMIT (verify 11/11 → trash deleted:1, not purge) → rollback (un-trash +
revert + reparent) … throwaways deleted (net-zero).`
It creates a throwaway master+secondary+note, commits (trashes the secondary), then **rolls back** (un-trashes
+ reparents) and hard-deletes the throwaways. If it does **not** end in `PASS`, **STOP** — do not proceed to
real data. Investigate. (The script pops the enable token in `finally`; it never persists.)

---

## 3. Go sequence

### 1. Set BOTH gates (out of band — the driver never sets them)
```powershell
$env:ZOT_MERGE_LIVE_ENABLED = "I-UNDERSTAND-LIVE-MERGE"   # engine C-1 owner token
$env:ZOT_LIVE_APPLY_GATE     = "I-UNDERSTAND"             # driver-local belt-and-suspenders
```

### 2. Run the driver — it STOPS after the first committed cluster
```powershell
uv run python scripts/phase2_live_apply.py --live
```
It commits **one** cluster, then HALTS and prints `[FIRST-STOP]` with the survivor (master) key, the trashed
dup key(s), the `snapshot_id` / PROV id, and a live re-read (secondary `trashed_not_purged=True` + `present`,
master `dc:replaces → dup`). The checkpoint `exit-gate-runs/apply/live-checkpoint.jsonl` now has one
`committed` line.

### 3. Inspect the one real merge in Zotero (do this before continuing)
- The **survivor** carries the union (collections/tags), any approved field enrichment, and the accumulated
  `tex.ids` citekey alias — the pinned citekey is preserved (verify check #11).
- The **dup** is in **Trash** (present, recoverable), not gone.
- The child note/attachment now hangs off the survivor.
If anything looks wrong, **ABORT** (§4) — the single merge is rolled back via the drill/`rollback_merge`; do
not continue.

### 4. Authorize the rest — re-invoke with `--continue-after-first`
```powershell
uv run python scripts/phase2_live_apply.py --live --continue-after-first
```
It resumes (auto-skips the already-committed first cluster via the checkpoint), commits the remaining ~150
confirmed clusters sequentially, refreshes the observability marker on a cadence, and prints a quantitative
end report (`committed / excluded / skipped`, secondaries trashed, checkpoint path, wall-clock). Monitor the
stream; each cluster prints `[COMMIT] … trashed=… prov=…`.

> Tunables (optional): `ZOT_LIVE_INTER_CLUSTER_SLEEP` (seconds between commits; default 0 — the gateway
> already honors `Backoff`/`Retry-After`) and `ZOT_LIVE_REFRESH_EVERY` (refresh the daily-report marker every
> N commits; default 20). The driver primes and refreshes observability itself — no separate producer needed.

---

## 4. Abort criteria (STOP the run)

Abort immediately if any of these occur:
- Any cluster returns **`mode != "committed"`** — the driver **fail-stops automatically**, prints
  `[FAIL-STOP]` with the rollback state, and exits non-zero. That cluster's own PATCH/trash was already
  routed to `rollback_merge` inside `commit_merge`; nothing past it is processed.
- Any **observability-degraded / blocked** status (freshness gate, ceiling, disjoint-cluster lock).
- Any **operator-observed wrong survivor** or unexpected trash during monitoring.

On abort: the **checkpoint preserves progress** (committed clusters are marked and auto-skipped on resume).
Investigate the printed reason. Do **not** resume until the cause is understood; when ready, re-run
`--live --continue-after-first` (it skips what already committed).

---

## 5. Post steps

```powershell
Remove-Item Env:\ZOT_MERGE_LIVE_ENABLED
Remove-Item Env:\ZOT_LIVE_APPLY_GATE
```
- [ ] **Unset both gates** (above) — never leave the live token set.
- [ ] Confirm the end report: `committed` == expected (~151), `excluded` == 1 (the uncertain pair),
      `blocked`/`rolled_back` == 0.
- [ ] Spot-check a few survivors + their trashed dups in Zotero; confirm citekeys intact.
- [ ] Merge the branch and push:
      ```powershell
      git -C C:\Users\rcesa\zotero-write-mcp checkout phase-2-merge-live
      git -C C:\Users\rcesa\zotero-write-mcp merge phase-2-dedup-hardening
      git -C C:\Users\rcesa\zotero-write-mcp push
      ```
- [ ] Append a build-log entry (harness repo) recording the live run: clusters committed, secondaries
      trashed, the checkpoint path, and the excluded pair carried forward.
- [ ] Resolve the excluded uncertain cluster `256MZBTC/ASJD3SKQ` separately (owner decision).
