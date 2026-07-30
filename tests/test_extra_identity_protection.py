"""`extra` identity protection + honest shadow/preview labelling (S5b Stage-E #2, V11-01 re-graded).

WHAT WAS FOUND. `_PROTECTED_FIELDS` is `frozenset({"citationKey"})` and its comment states the right
intent — "the survivor's pinned citation key IS its identity" — but Better BibTeX pins the real key
INSIDE `extra` as a `Citation Key:` line. So `field_sources={"extra": <secondary>}` walked past the
guard and replaced the survivor's pinned key and `tex.ids` aliases with the duplicate's.

WHAT IT WAS NOT. Originally filed as a stop-the-line BLOCKER ("passes all 11 checks yet loses data").
That grading was WRONG and the owner caught it. Measured across three arms holding the merge constant:

    static-dict get_citekey + observed=projection   -> #11 passes   (the original repro)
    production get_citekey  + observed=projection   -> #11 passes   (so the reader was NOT the variable)
    production get_citekey  + observed=build_cluster -> #11 FAILS
        detail: observed='anonUntitled2001' snapshot='sandersBasinMexico1979'

The tautology lived in `compute_merge_projection`, which sets `citekey=sm.citekey` and never re-derives
the key from its own overridden `extra`. The LIVE gate re-reads (`merge_live` -> `build_cluster`) and
catches the clobber in every shape tested, including a survivor with only a computed `citationKey` and
an honestly-keyless survivor. So the real defect is FAIL-SAFE: a merge that looks green in preview and
can never commit. Both halves are fixed and pinned below — identity preservation, and a preview that
admits it is not a gate result.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from zotero_write_mcp.merge import (
    _citekey_from_extra,
    _extra_preserving_identity,
    _tex_ids_of,
    build_cluster,
    compute_merge_projection,
    shadow_merge,
    snapshot_cluster,
    verify_merge,
)
from zotero_write_mcp.provenance import ProvenanceStore

MASTER_EXTRA = "Citation Key: sandersBasinMexico1979\ntex.ids: sanders1979basin"
DUP_EXTRA = "Citation Key: anonUntitled2001\nPMID: 12345"


def _item(key, version, item_type="journalArticle", **data):
    d = {"key": key, "version": version, "itemType": item_type, **data}
    return {"key": key, "version": version, "data": d}


class ProductionReader:
    """get_citekey byte-equivalent to merge_live.WebClusterReader.get_citekey — it DERIVES the key from
    the item's current `extra`. A fake that returns None, or a static dict keyed by item key, cannot
    detect an extra-clobber and produces a fake green; that is the trap this file exists to avoid."""

    def __init__(self, items):
        self._items = items

    def get_item(self, key):
        return self._items[key]

    def get_children(self, key):
        return []

    def get_annotations(self, attachment_key):
        return []

    def get_citekey(self, key):
        data = self.get_item(key).get("data", {}) or {}
        return _citekey_from_extra(data.get("extra")) or data.get("citationKey") or None


def _cluster(tmp_path, master_extra=MASTER_EXTRA, master_citationkey=None):
    md = dict(title="Basin of Mexico", collections=["C1"], tags=[], relations={})
    if master_extra is not None:
        md["extra"] = master_extra
    if master_citationkey is not None:
        md["citationKey"] = master_citationkey
    items = {
        "M1": _item("M1", 100, **md),
        "M2": _item("M2", 101, title="Basin of Mexico dup", collections=["C1"], tags=[],
                    relations={}, extra=DUP_EXTRA),
    }
    reader = ProductionReader(items)
    snap = snapshot_cluster(reader, "M1", ["M2"], prov=ProvenanceStore(tmp_path))
    return items, reader, snap


# ── the unit: identity preserved, content taken ─────────────────────────────────────────────────

def test_survivors_pinned_key_survives_an_extra_override():
    out = _extra_preserving_identity(MASTER_EXTRA, DUP_EXTRA)
    assert _citekey_from_extra(out) == "sandersBasinMexico1979"


def test_duplicates_pinned_key_is_not_inherited():
    out = _extra_preserving_identity(MASTER_EXTRA, DUP_EXTRA)
    assert "anonUntitled2001" not in out


def test_non_identity_content_IS_taken_from_the_source():
    """The point of the override is still honoured — only identity is withheld."""
    out = _extra_preserving_identity(MASTER_EXTRA, DUP_EXTRA)
    assert "PMID: 12345" in out


def test_survivors_own_aliases_survive():
    out = _extra_preserving_identity(MASTER_EXTRA, DUP_EXTRA)
    assert "sanders1979basin" in _tex_ids_of(out)


def test_a_pinless_survivor_does_not_acquire_a_pinned_key():
    """A survivor whose key is COMPUTED (no extra line — ~73% of this library) must not gain a pinned
    line from a duplicate; that is how a computed key silently becomes the wrong pinned key."""
    out = _extra_preserving_identity(None, DUP_EXTRA)
    assert _citekey_from_extra(out) is None
    assert "PMID: 12345" in out


def test_source_aliases_are_not_smuggled_in_as_identity():
    out = _extra_preserving_identity("Citation Key: keepMe", "tex.ids: sneakyA, sneakyB\nnote")
    assert _tex_ids_of(out) == []
    assert _citekey_from_extra(out) == "keepMe"


def test_extra_pinned_dup_key_is_accumulated_as_alias(tmp_path):
    """Review finding F-4: a duplicate whose BBT key is pinned ONLY via the `extra`
    `Citation Key:` line (the dominant pinning shape — no `citationKey` field) must have that key
    preserved as a `tex.ids` alias on the survivor, so `@dupkey` citations keep resolving after the
    merge. It must NOT become the survivor's pinned identity."""
    from zotero_write_mcp.merge import compute_merge_projection
    _, _, snap = _cluster(tmp_path)                       # dup extra pins anonUntitled2001, no field
    proj = compute_merge_projection(snap)
    out = proj.items["M1"].fields.get("extra") or ""
    assert _citekey_from_extra(out) == "sandersBasinMexico1979", "survivor identity unchanged"
    assert "anonUntitled2001" in _tex_ids_of(out), \
        "the dup's extra-pinned key must survive as a tex.ids alias"
    assert "sanders1979basin" in _tex_ids_of(out), "pre-existing aliases keep accumulating"


# ── the live gate: the scenario that was mis-filed as a BLOCKER now passes it ────────────────────

def _live_verify(items, snap, field_sources):
    """Apply the merge's own override to the master, then RE-READ and verify exactly as
    merge_live._commit_merge_inner does (observed = build_cluster(reader, ...))."""
    from zotero_write_mcp.merge import _master_overrides
    overrides = _master_overrides(snap, field_sources)
    post = {k: {"key": v["key"], "version": v["version"], "data": dict(v["data"])}
            for k, v in items.items()}
    post["M1"]["data"].update(overrides)
    observed = build_cluster(ProductionReader(post), "M1", ["M2"])
    return observed, verify_merge(snap, observed, field_sources=field_sources)


def _check(report, number):
    return next(c for c in report.checks if c.number == number)


def test_extra_override_now_passes_check_11_against_a_LIVE_reread(tmp_path):
    """Before the fix this failed #11 (observed='anonUntitled2001' vs snapshot='sandersBasin…') — a
    merge that could be previewed green and never committed. It must now pass honestly, by preserving
    the key rather than by weakening the check."""
    items, _, snap = _cluster(tmp_path)
    observed, report = _live_verify(items, snap, {"extra": "M2"})
    c11 = _check(report, 11)
    assert c11.passed, f"#11 still fails: {c11.detail}"
    assert observed.items["M1"].citekey == "sandersBasinMexico1979"


def test_the_duplicates_key_is_kept_as_an_ALIAS_not_as_identity(tmp_path):
    """The sanctioned way a merged-away key keeps resolving: alias accumulation, not identity theft."""
    items, _, snap = _cluster(tmp_path)
    observed, _ = _live_verify(items, snap, {"extra": "M2"})
    extra = observed.items["M1"].fields["extra"]
    assert _citekey_from_extra(extra) == "sandersBasinMexico1979"
    assert "sanders1979basin" in _tex_ids_of(extra)


def test_pinless_survivor_extra_override_passes_the_live_gate(tmp_path):
    """The ARM D shape: survivor carries only a computed citationKey. Importing the dup's pinned line
    used to fail #11; preserving identity means the computed key still wins."""
    items, _, snap = _cluster(tmp_path, master_extra=None,
                             master_citationkey="sandersBasinMexico1979")
    observed, report = _live_verify(items, snap, {"extra": "M2"})
    c11 = _check(report, 11)
    assert c11.passed, f"#11 still fails: {c11.detail}"
    assert observed.items["M1"].citekey == "sandersBasinMexico1979"


def test_check_11_still_bites_a_genuine_citekey_change(tmp_path):
    """Guard against fixing V11-01 by blunting #11: an actual key change must STILL fail."""
    items, _, snap = _cluster(tmp_path)
    post = {k: {"key": v["key"], "version": v["version"], "data": dict(v["data"])}
            for k, v in items.items()}
    post["M1"]["data"]["extra"] = "Citation Key: somethingElseEntirely2020"
    observed = build_cluster(ProductionReader(post), "M1", ["M2"])
    assert _check(verify_merge(snap, observed), 11).passed is False


# ── honest shadow/preview labelling ─────────────────────────────────────────────────────────────

def test_shadow_against_the_projection_is_labelled_NOT_gate_authoritative(tmp_path):
    """The real user-facing harm behind V11-01: a projection-only pass read as a commit verdict lets an
    operator approve a merge that can never land. It must say what it verified against."""
    with tempfile.TemporaryDirectory() as td:
        items, reader, _ = _cluster(Path(td) / "a")
        sr = shadow_merge(reader, "M1", ["M2"], prov=ProvenanceStore(Path(td) / "b"))
    assert sr.verified_against == "projection"
    assert sr.is_gate_authoritative is False


def test_shadow_against_a_real_reread_IS_gate_authoritative(tmp_path):
    with tempfile.TemporaryDirectory() as td:
        items, reader, snap = _cluster(Path(td) / "a")
        observed, _ = _live_verify(items, snap, {"extra": "M2"})
        sr = shadow_merge(reader, "M1", ["M2"], prov=ProvenanceStore(Path(td) / "b"),
                          observed=observed)
    assert sr.verified_against == "observed"
    assert sr.is_gate_authoritative is True


def test_projection_citekey_is_carried_verbatim_documenting_the_tautology(tmp_path):
    """The mechanism that made the original repro look like a BLOCKER, kept executable so nobody
    re-derives it the hard way: compute_merge_projection carries citekey=sm.citekey, so check #11 is
    tautological against the projection even when `extra` was replaced wholesale."""
    items, _, snap = _cluster(tmp_path)
    # Bypass identity preservation to recreate the original clobber shape.
    snap.items["M2"].fields["extra"] = DUP_EXTRA
    proj = compute_merge_projection(snap, field_sources={"extra": "M2"})
    assert proj.items["M1"].citekey == snap.items["M1"].citekey
    assert _check(verify_merge(snap, proj, field_sources={"extra": "M2"}), 11).passed is True
