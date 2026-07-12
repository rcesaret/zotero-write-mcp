"""S5a F1 — pre-live readiness checks. Every function here is READ-ONLY: no Zotero writes, no PROV
writes (beyond the store's own directory-writable probe file, which is created and immediately
removed), no env mutation. Each row reports a ``status`` in ``{"pass","warn","fail"}`` plus enough
detail for a human or ``scripts/doctor.ps1``/``.sh`` to act on. ``readiness_report()`` composes all
rows into one JSON blob for ``scripts/readiness.py``.

Two owner-facing verdicts, not just raw rows: "is a live MERGE safe right now" (observability
freshness + PROV writable) and "is a live CREATE safe right now" (local-API latency + PROV writable) —
the S4/S4-CLOSE field finding that these are two DIFFERENT questions with different failure modes.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from zotero_write_mcp.merge_live import ENABLE_ENV, ENABLE_TOKEN, DEFAULT_FRESHNESS_WINDOW
from zotero_write_mcp.observability import latest_daily_report, observability_is_fresh
from zotero_write_mcp.provenance import ProvenanceStore


def live_merge_mode_row() -> dict:
    """Which mode a live ``commit_merge`` would take RIGHT NOW: live-capable vs. shadow-only. The
    token is an out-of-band env var (ADR-005/C-1) — this row only reads it, never sets it."""
    enabled = os.environ.get(ENABLE_ENV) == ENABLE_TOKEN
    return {
        "row": "live_merge_mode",
        "status": "warn" if enabled else "pass",
        "enabled": enabled,
        "detail": (f"{ENABLE_ENV} IS SET to the live token — commit_merge WOULD attempt a live trash."
                  if enabled else
                  f"{ENABLE_ENV} is unset — commit_merge runs SHADOW-only (no trash) right now."),
    }


def observability_freshness_row(prov: ProvenanceStore, *, window_seconds: float = DEFAULT_FRESHNESS_WINDOW,
                                now: Optional[datetime] = None) -> dict:
    """Whether ``commit_merge``'s C-2 freshness gate would pass right now (a stale/absent/degraded
    ``daily_report`` marker fails a live commit closed even with the token set)."""
    now = now or datetime.now(timezone.utc)
    rep = latest_daily_report(prov)
    fresh = observability_is_fresh(prov, window_seconds=window_seconds, now=now)
    return {
        "row": "observability_freshness",
        "status": "pass" if fresh else "fail",
        "fresh": fresh,
        "latest_report_ts": (rep or {}).get("ts"),
        "latest_report_status": ((rep or {}).get("params") or {}).get("status"),
        "window_seconds": window_seconds,
        "detail": ("commit_merge's C-2 gate would PASS the freshness check right now." if fresh else
                  "commit_merge's C-2 gate would BLOCK any live commit right now "
                  "(stale, absent, or degraded daily_report — run merge_health_report first)."),
    }


def prov_store_row(prov: ProvenanceStore) -> dict:
    """The PROV store's directory is writable and reports its live record count. A non-writable PROV
    dir means every mutation attempted from here would be unrollbackable (ADR-008 interlock)."""
    try:
        prov.root.mkdir(parents=True, exist_ok=True)
        probe = prov.root / ".readiness-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except Exception:
        writable = False
    return {
        "row": "prov_store",
        "status": "pass" if writable else "fail",
        "writable": writable,
        "record_count": prov.count(),
        "root": str(prov.root),
        "detail": (f"PROV store writable at {prov.root} ({prov.count()} records)." if writable else
                  f"PROV store at {prov.root} is NOT writable — every mutation here "
                  "would be unrollbackable."),
    }


def _git_head(repo_root: Path) -> tuple:
    try:
        branch = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip() or None
        commit = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip() or None
        return commit, branch
    except Exception:
        return None, None


def engine_version_skew_row() -> dict:
    """Which install the ``zotero_write_mcp`` import actually resolves — the EDITABLE dev tree (a
    ``.pth`` into a git checkout) vs. an installed build (uv-tool/site-packages) — plus, for the dev
    tree, its checked-out branch/commit. The S4-CLOSE field finding, made a permanent readiness check
    (S5a POST-supplement #3a): a running server can silently keep serving a PRE-merge import until
    restarted, so this row also flags that possibility for the operator to judge."""
    import zotero_write_mcp as _zwm
    resolved_file = getattr(_zwm, "__file__", None)
    installed_version = getattr(_zwm, "__version__", None)
    is_editable = bool(resolved_file and (os.sep + "src" + os.sep) in resolved_file)
    pyproject_version = None
    git_commit = git_branch = None
    if resolved_file and is_editable:
        pkg_dir = Path(resolved_file).resolve().parent          # .../src/zotero_write_mcp
        repo_root = pkg_dir.parent.parent                        # .../src/.. -> repo root
        pyproject = repo_root / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version"):
                    pyproject_version = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
            git_commit, git_branch = _git_head(repo_root)
    skew = pyproject_version is not None and pyproject_version != installed_version
    return {
        "row": "engine_deployment",
        "status": "warn" if skew else "pass",
        "resolved_file": resolved_file,
        "installed_version": installed_version,
        "pyproject_version": pyproject_version,
        "is_editable_dev_tree": is_editable,
        "git_branch": git_branch,
        "git_commit": git_commit,
        "detail": (
            f"Serving EDITABLE dev tree at {resolved_file!r} on branch {git_branch!r} @ {git_commit!r}. "
            "This process's own import is current as of process start — a separate long-running "
            "server process may still hold an OLDER import until it is restarted."
            if is_editable else
            f"Serving an installed build (not the editable dev tree) at {resolved_file!r}."
        ),
    }


def local_api_latency_row(*, url: Optional[str] = None, timeout: float = 5.0,
                          pass_seconds: float = 2.5) -> dict:
    """Probes the Zotero local API's title-search latency — hook #3's live-create budget needs
    < 2.5s per request; this host has shown 6-35s under a sustained write-transaction / journal-file
    load (S4 / S4-CLOSE field finding). Read-only GET; never raises — a connection failure or timeout
    is reported as a row, not an exception."""
    import httpx
    base = (url or os.environ.get("ZOTERO_LOCAL_URL", "http://127.0.0.1:23119/api")).rstrip("/")
    lib = os.environ.get("ZOTERO_LIBRARY_ID", "0")
    path = f"{base}/users/{lib}/items"
    t0 = time.monotonic()
    try:
        r = httpx.get(path, params={"q": "test", "qmode": "titleCreatorYear", "limit": 25, "format": "json"},
                      headers={"Zotero-Allowed-Request": "true"}, timeout=timeout)
        elapsed = time.monotonic() - t0
        ok = r.status_code == 200 and elapsed < pass_seconds
        return {
            "row": "local_api_latency", "status": "pass" if ok else "warn",
            "elapsed_seconds": round(elapsed, 3), "http_status": r.status_code,
            "pass_bar_seconds": pass_seconds,
            "detail": (f"titleCreatorYear search returned in {elapsed:.2f}s (< {pass_seconds}s bar) — "
                      "live create/dedup-gate reads should clear hook #3's budget."
                      if ok else
                      f"titleCreatorYear search took {elapsed:.2f}s (bar is {pass_seconds}s, HTTP "
                      f"{r.status_code}) — hook #3 will likely fail-close on a live create right now."),
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {
            "row": "local_api_latency", "status": "fail",
            "elapsed_seconds": round(elapsed, 3), "error": f"{type(e).__name__}: {e}",
            "pass_bar_seconds": pass_seconds,
            "detail": f"Local API unreachable/timed out after {elapsed:.2f}s — {type(e).__name__}: {e}",
        }


def readiness_report(prov: ProvenanceStore, *, probe_local_api: bool = True) -> dict:
    """Compose every F1 row into one report plus the two owner-facing verdicts."""
    rows = [
        live_merge_mode_row(),
        observability_freshness_row(prov),
        prov_store_row(prov),
        engine_version_skew_row(),
    ]
    if probe_local_api:
        rows.append(local_api_latency_row())
    by_row = {r["row"]: r for r in rows}
    live_merge_safe_now = (
        by_row["observability_freshness"]["status"] == "pass"
        and by_row["prov_store"]["status"] == "pass"
    )
    live_create_safe_now = None
    if probe_local_api:
        live_create_safe_now = (
            by_row.get("local_api_latency", {}).get("status") == "pass"
            and by_row["prov_store"]["status"] == "pass"
        )
    return {
        "rows": rows,
        "live_merge_safe_now": live_merge_safe_now,
        "live_create_safe_now": live_create_safe_now,
    }
