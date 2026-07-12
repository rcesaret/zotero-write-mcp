"""Unit tests for S5a F1 — pre-live readiness rows (readiness.py). All read-only; local_api_latency_row
is tested against a monkeypatched httpx.get so the suite never depends on a real Zotero process."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from zotero_write_mcp import readiness
from zotero_write_mcp.merge_live import ENABLE_ENV, ENABLE_TOKEN
from zotero_write_mcp.observability import daily_report
from zotero_write_mcp.provenance import ProvenanceStore


# ── live_merge_mode_row ─────────────────────────────────────────────

def test_live_merge_mode_pass_when_unset(monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    row = readiness.live_merge_mode_row()
    assert row["status"] == "pass" and row["enabled"] is False


def test_live_merge_mode_warn_when_set(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, ENABLE_TOKEN)
    row = readiness.live_merge_mode_row()
    assert row["status"] == "warn" and row["enabled"] is True


def test_live_merge_mode_pass_when_wrong_value(monkeypatch):
    """A near-miss token value is NOT enabled (the token is an exact-match out-of-band gate)."""
    monkeypatch.setenv(ENABLE_ENV, "I-UNDERSTAND-LIVE-MERGE-typo")
    row = readiness.live_merge_mode_row()
    assert row["status"] == "pass" and row["enabled"] is False


# ── observability_freshness_row ─────────────────────────────────────

def test_observability_freshness_pass_when_fresh(tmp_path):
    prov = ProvenanceStore(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
    daily_report(prov, ts=(now - timedelta(hours=1)).isoformat())
    row = readiness.observability_freshness_row(prov, now=now)
    assert row["status"] == "pass" and row["fresh"] is True


def test_observability_freshness_fail_when_stale(tmp_path):
    prov = ProvenanceStore(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
    daily_report(prov, ts=(now - timedelta(hours=100)).isoformat())
    row = readiness.observability_freshness_row(prov, now=now)
    assert row["status"] == "fail" and row["fresh"] is False


def test_observability_freshness_fail_when_absent(tmp_path):
    prov = ProvenanceStore(tmp_path)
    row = readiness.observability_freshness_row(prov)
    assert row["status"] == "fail" and row["latest_report_ts"] is None


# ── prov_store_row ───────────────────────────────────────────────────

def test_prov_store_row_writable_and_counts(tmp_path):
    prov = ProvenanceStore(tmp_path)
    prov.record(activity="snapshot_cluster", item_key="A")
    row = readiness.prov_store_row(prov)
    assert row["status"] == "pass" and row["writable"] is True
    assert row["record_count"] == 1


# ── engine_version_skew_row ──────────────────────────────────────────

def test_engine_version_skew_row_returns_expected_shape():
    row = readiness.engine_version_skew_row()
    assert row["row"] == "engine_deployment"
    assert row["status"] in {"pass", "warn"}
    assert "resolved_file" in row and "is_editable_dev_tree" in row


# ── local_api_latency_row ────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def test_local_api_latency_pass_when_fast(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(200))
    row = readiness.local_api_latency_row(pass_seconds=2.5)
    assert row["status"] == "pass"
    assert row["http_status"] == 200


def test_local_api_latency_warn_when_slow(monkeypatch):
    import time as _time

    def slow_get(*a, **k):
        return _FakeResponse(200)

    real_monotonic = _time.monotonic
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        # first call = t0, second call (after "request") = t0 + 6s
        return real_monotonic() if calls["n"] == 1 else real_monotonic() + 6.0

    monkeypatch.setattr(httpx, "get", slow_get)
    monkeypatch.setattr(readiness.time, "monotonic", fake_monotonic)
    row = readiness.local_api_latency_row(pass_seconds=2.5)
    assert row["status"] == "warn"
    assert row["elapsed_seconds"] >= 2.5


def test_local_api_latency_fail_on_exception(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    row = readiness.local_api_latency_row()
    assert row["status"] == "fail"
    assert "refused" in row["error"]


# ── readiness_report composition ─────────────────────────────────────

def test_readiness_report_verdicts_true_when_all_pass(tmp_path, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    prov = ProvenanceStore(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
    daily_report(prov, ts=(now - timedelta(hours=1)).isoformat())
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(200))
    rep = readiness.readiness_report(prov, probe_local_api=True)
    assert len(rep["rows"]) == 5
    assert rep["live_merge_safe_now"] is True
    # live_create_safe_now depends on the fast fake response's near-zero elapsed time
    assert rep["live_create_safe_now"] is True


def test_readiness_report_merge_unsafe_when_observability_stale(tmp_path, monkeypatch):
    prov = ProvenanceStore(tmp_path)   # no daily_report at all -> stale/absent
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(200))
    rep = readiness.readiness_report(prov)
    assert rep["live_merge_safe_now"] is False


def test_readiness_report_skips_local_api_probe_when_disabled(tmp_path):
    prov = ProvenanceStore(tmp_path)
    rep = readiness.readiness_report(prov, probe_local_api=False)
    assert len(rep["rows"]) == 4
    assert rep["live_create_safe_now"] is None
