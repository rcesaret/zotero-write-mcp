"""LIVE network smoke for the Phase-3 validation source clients (sprint S-V1, exit-gate check 6).

Resolves ONE known real DOI against the live bibliographic authorities and proves:
  * DOI content-negotiation + Crossref (keyless, MANDATORY) both resolve and AGREE on title + year,
  * OpenAlex is included when OPENALEX_API_KEY is present (additive; a missing key degrades, never fails),
  * a repeat run is served entirely from the content-addressed cache with ZERO new network calls.

This is a MANUAL, network-touching script — it is NOT collected by pytest (it lives under scripts/ and
is gated behind the ZOT_SOURCES_LIVE env flag), so the offline suite never depends on it.

  READ-ONLY: this touches only external authorities. It makes ZERO writes to Zotero by any path.

Run (Git-Bash):   ZOT_SOURCES_LIVE=1 uv run --with httpx python scripts/sources_live_smoke.py
Run (PowerShell): $env:ZOT_SOURCES_LIVE=1; uv run --with httpx python scripts/sources_live_smoke.py

Exit 0 = PASS (>=2 keyless authorities agree, cache-hit on repeat). Non-zero = FAIL.
"""
import os
import shutil
import sys
from pathlib import Path

from zotero_write_mcp.dedup import normalize_title
from zotero_write_mcp.sources import (
    HttpxReadTransport,
    JsonCache,
    default_authorities,
    gather_by_doi,
)

# Kohler, Smith et al. 2017, Nature — "Greater post-Neolithic wealth disparities in Eurasia than in
# North America and Mesoamerica". Archaeology/demography, ASU-authored, indexed by Crossref + OpenAlex
# + DOI content-negotiation. A stable, maximally-indexed DOI.
SMOKE_DOI = "10.1038/nature24646"


class CountingTransport:
    """Wraps the live transport to count network calls (proves the repeat run hits zero network)."""

    def __init__(self, inner):
        self._inner = inner
        self.count = 0

    def request(self, method, url, *, headers=None, params=None):
        self.count += 1
        return self._inner.request(method, url, headers=headers, params=params)


def _title_year(rec):
    return (normalize_title(rec.title), rec.year)


def main() -> int:
    if not os.environ.get("ZOT_SOURCES_LIVE"):
        print("SKIP: set ZOT_SOURCES_LIVE=1 to run the live network smoke (no-op without it).")
        return 0

    key_set = bool(os.environ.get("OPENALEX_API_KEY"))
    print(f"OPENALEX_API_KEY {'SET (OpenAlex leg additive)' if key_set else 'ABSENT (degraded: DOI-neg + Crossref only)'}")

    # Fresh cache under runtime/ (gitignored) so the idempotency proof starts clean.
    cache_dir = Path("runtime") / "validation-cache" / "live-smoke"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    transport = CountingTransport(HttpxReadTransport())
    cache = JsonCache(cache_dir)
    authorities = default_authorities(transport=transport, cache=cache)

    # ── Run 1: live resolution ────────────────────────────────────────────────
    print(f"\n=== RUN 1 (live) — resolving {SMOKE_DOI} ===")
    res = gather_by_doi(SMOKE_DOI, authorities)
    for note in res.evidence:
        print(f"  {note}")
    print(f"  answered authorities: {res.answered}")
    for rec in res.records:
        nt, yr = _title_year(rec)
        print(f"  [{rec.source}] year={yr!r} doi={rec.doi!r} title~={nt[:60]!r}")
    run1_calls = transport.count
    print(f"  network calls in run 1: {run1_calls}")

    # ── Run 2: must be served entirely from cache ─────────────────────────────
    print("\n=== RUN 2 (repeat) — must be served from cache, zero new network ===")
    before = transport.count
    res2 = gather_by_doi(SMOKE_DOI, authorities)
    new_calls = transport.count - before
    print(f"  new network calls in run 2: {new_calls}")

    # ── Agreement + cache assertions ──────────────────────────────────────────
    answered = {r.source: r for r in res.records}
    mandatory = [a for a in ("doi_negotiation", "crossref") if a in answered]
    print("\n=== VERDICT ===")

    ok = True
    if len(mandatory) < 2:
        print(f"  FAIL: fewer than 2 keyless authorities resolved (got {mandatory}).")
        ok = False
    else:
        dn, cr = answered["doi_negotiation"], answered["crossref"]
        year_ok = dn.year == cr.year and dn.year != ""
        from difflib import SequenceMatcher
        sim = SequenceMatcher(None, normalize_title(dn.title), normalize_title(cr.title)).ratio()
        title_ok = sim >= 0.90
        print(f"  DOI-neg vs Crossref: year_match={year_ok} (={dn.year!r}), title_similarity={sim:.3f}")
        if not (year_ok and title_ok):
            print("  FAIL: keyless authorities disagree on title/year.")
            ok = False
        # OpenAlex leg (additive)
        if "openalex" in answered:
            oa = answered["openalex"]
            print(f"  OpenAlex leg: PRESENT — year={oa.year!r} agrees={oa.year == dn.year}")
        elif key_set:
            print("  OpenAlex leg: key SET but OpenAlex did not resolve (still pass on the 2 keyless legs).")
        else:
            print("  OpenAlex leg: skipped (no key) — DEGRADED but non-blocking.")

    if new_calls != 0:
        print(f"  FAIL: repeat run touched the network ({new_calls} new calls) — cache not serving.")
        ok = False
    else:
        print(f"  cache-hit on repeat: {len(res2.records)} record(s) served with 0 new network calls.")

    tier = ">=3 (key set)" if key_set and "openalex" in answered else ">=2 keyless (degraded-ok)"
    print(f"\n{'PASS' if ok else 'FAIL'} — agreement tier: {tier}; authorities answered: {res.answered}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
