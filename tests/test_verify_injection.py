"""ADVERSARIAL INJECTION SUITE for verify_merge — INDEPENDENT red-team at the Phase-1 gate.

Goal (per the merge-safety rule and the Phase-1 adversarial-injection exit gate):
  1. For EACH of the 12 named checks, drive a state that violates EXACTLY that one invariant and
     prove the gate fails-closed naming that check.
  2. HUNT for gate holes: corrupt `observed` in ways the gate might PASS. Where the gate catches it,
     it lands as a passing injection test; where the gate MISSES it, it is recorded as an
     `xfail(strict=False)` with a precise GATE HOLE annotation and reported to the owner.

This suite deliberately builds a DIFFERENT, RICHER cluster than tests/test_verify_merge.py:
  * Master M + TWO secondaries S1, S2 (3-item cluster).
  * Each item carries >=2 collections (overlapping unions exercised).
  * Master has its own pre-existing relation; S2 carries a second pre-existing predicate.
  * Multiple attachments across the cluster (one under M, one under each secondary).
  * >=2 annotation grandchildren (two under the master's attachment).
  * Notes under M and under S1.
The cluster is constructed so its secondary keys are SUBSTRING-COLLIDING by design
(`S1` is a substring of `S1X`) to probe the dc:replaces substring matcher (probe (c)).

NOTHING in this file imports from or reuses tests/test_verify_merge.py. Pure, offline, no network.
"""
import copy

import pytest

from zotero_write_mcp.merge import (
    snapshot_cluster, compute_merge_projection, verify_merge,
)
from zotero_write_mcp.provenance import ProvenanceStore

LIBRARY_BASE = "http://zotero.org/users/0/items"


# ── Fake reader (independent; not the one in test_verify_merge.py) ───────────────

class FakeReader:
    """Minimal injectable ClusterReader over in-memory dicts."""

    def __init__(self, items, children, annotations, citekeys):
        self._items = items
        self._children = children
        self._annotations = annotations
        self._citekeys = citekeys

    def get_item(self, key):
        return self._items[key]

    def get_children(self, key):
        return self._children.get(key, [])

    def get_annotations(self, attachment_key):
        return self._annotations.get(attachment_key, [])

    def get_citekey(self, key):
        return self._citekeys.get(key)


def _wrap(key, version, item_type="journalArticle", **data):
    d = {"key": key, "version": version, "itemType": item_type, **data}
    return {"key": key, "version": version, "data": d}


# Substring-colliding secondary keys: "S1" is a proper substring of "S1X".
MASTER = "M"
SEC1 = "S1"
SEC2 = "S1X"


def _build(tmp_path):
    """A 3-item cluster (M + S1 + S1X), >=2 collections each, 2 pre-existing relations,
    3 attachments, notes under M and S1, and 2 annotation grandchildren under M's attachment."""
    items = {
        MASTER: _wrap(
            MASTER, 100, title="Teotihuacan and the Basin", date="1979",
            collections=["COL_A", "COL_B"],
            tags=[{"tag": "aztec", "type": 1}, {"tag": "demography", "type": 1}],
            relations={"dc:relation": [f"{LIBRARY_BASE}/EXT0"]},
        ),
        SEC1: _wrap(
            SEC1, 101, title="Teotihuacan (dup A)", date="1979",
            collections=["COL_B", "COL_C"],
            tags=[{"tag": "maya", "type": 1}],
            relations={},
        ),
        SEC2: _wrap(
            SEC2, 102, title="Teotihuacan (dup B)", date="1979",
            collections=["COL_C", "COL_D"],
            tags=[{"tag": "highland", "type": 0}],
            relations={"owl:relatedTo": [f"{LIBRARY_BASE}/EXT9"]},
        ),
    }
    children = {
        MASTER: [
            _wrap("N_M", 110, item_type="note", parentItem=MASTER, note="master note"),
            _wrap("ATT_M", 111, item_type="attachment", parentItem=MASTER,
                  md5="md5_m", filename="m.pdf", contentType="application/pdf"),
        ],
        SEC1: [
            _wrap("N_S1", 120, item_type="note", parentItem=SEC1, note="s1 note"),
            _wrap("ATT_S1", 121, item_type="attachment", parentItem=SEC1,
                  md5="md5_s1", filename="s1.pdf", contentType="application/pdf"),
        ],
        SEC2: [
            _wrap("ATT_S2", 130, item_type="attachment", parentItem=SEC2,
                  md5="md5_s2", filename="s2.pdf", contentType="application/pdf"),
        ],
    }
    annotations = {
        "ATT_M": [
            _wrap("ANN_1", 140, item_type="annotation", parentItem="ATT_M", annotationText="a1"),
            _wrap("ANN_2", 141, item_type="annotation", parentItem="ATT_M", annotationText="a2"),
        ],
        "ATT_S1": [
            _wrap("ANN_3", 150, item_type="annotation", parentItem="ATT_S1", annotationText="a3"),
        ],
        "ATT_S2": [],
    }
    citekeys = {MASTER: "teotihuacanBasin1979", SEC1: "teoDupA1979", SEC2: "teoDupB1979"}
    reader = FakeReader(items, children, annotations, citekeys)
    return snapshot_cluster(reader, MASTER, [SEC1, SEC2], prov=ProvenanceStore(tmp_path))


@pytest.fixture
def snap(tmp_path):
    return _build(tmp_path)


def names(report):
    return {c.name for c in report.failed}


def project(snap, *, smart_fill=False):
    return copy.deepcopy(
        compute_merge_projection(snap, smart_fill=smart_fill, library_base=LIBRARY_BASE)
    )


# ── Sanity: the richer golden projection passes all 12 checks ────────────────────

def test_golden_passes_all_checks(snap):
    rep = verify_merge(snap, compute_merge_projection(snap, library_base=LIBRARY_BASE))
    assert rep.passed, rep.to_dict()
    assert len(rep.checks) >= 12          # 11 invariants, count-parity split into 2


# ════════════════════════════════════════════════════════════════════════════════
#  PART 1 — one isolated failure injection per invariant (12 checks)
# ════════════════════════════════════════════════════════════════════════════════

def test_inv01_item_type_equality(snap):
    p = project(snap)
    p.items[SEC2].item_type = "book"                       # a secondary is a different type
    r = verify_merge(snap, p)
    assert r.passed is False and "item-type-equality" in names(r)


def test_inv02_version_drift(snap):
    p = project(snap)
    p.items[SEC1].version = 9999                           # secondary edited mid-merge window
    r = verify_merge(snap, p)
    assert r.passed is False and "version-drift" in names(r)


def test_inv03_master_scalar_preservation(snap):
    p = project(snap)
    p.items[MASTER].fields["title"] = "CLOBBERED"          # survivor's own field overwritten
    r = verify_merge(snap, p)
    assert r.passed is False and "master-scalar-preservation" in names(r)


def test_inv04_collections_equality(snap):
    p = project(snap)
    p.items[MASTER].collections = ["COL_A", "COL_B"]       # dropped COL_C/COL_D from the union
    r = verify_merge(snap, p)
    assert r.passed is False and "collections-equality" in names(r)


def test_inv05_tags_tuple_superset(snap):
    p = project(snap)
    # drop the 'demography' tuple entirely
    p.items[MASTER].tags = [t for t in p.items[MASTER].tags if t[0] != "demography"]
    r = verify_merge(snap, p)
    assert r.passed is False and "tags-tuple-superset" in names(r)


def test_inv06_relations_superset_dc_replaces(snap):
    p = project(snap)
    p.items[MASTER].relations["dc:replaces"] = []          # forgot dc:replaces -> secondaries
    r = verify_merge(snap, p)
    assert r.passed is False and "relations-superset" in names(r)


def test_inv06b_relations_superset_preexisting_dropped(snap):
    p = project(snap)
    p.items[MASTER].relations.pop("owl:relatedTo", None)   # lost S2's pre-existing predicate
    r = verify_merge(snap, p)
    assert r.passed is False and "relations-superset" in names(r)


def test_inv07_child_completeness(snap):
    p = project(snap)
    for n in p.notes:
        if n.key == "N_S1":
            n.parent_key = "GHOST"                          # a child never reparented to master
    r = verify_merge(snap, p)
    assert r.passed is False and "child-completeness" in names(r)


def test_inv08_note_count_parity(snap):
    p = project(snap)
    p.notes = [n for n in p.notes if n.key != "N_S1"]       # a note vanished
    r = verify_merge(snap, p)
    assert r.passed is False and "note-count-parity" in names(r)


def test_inv08_attachment_count_parity(snap):
    p = project(snap)
    p.attachments = [a for a in p.attachments if a.key != "ATT_S2"]  # an attachment vanished
    r = verify_merge(snap, p)
    assert r.passed is False and "attachment-count-parity" in names(r)


def test_inv09_annotation_parity(snap):
    p = project(snap)
    for a in p.attachments:
        if a.key == "ATT_M":
            a.annotations = a.annotations[:1]               # lost one of master's two annotations
    r = verify_merge(snap, p)
    assert r.passed is False and "annotation-parity" in names(r)


def test_inv10_attachment_storage_integrity(snap):
    p = project(snap)
    for a in p.attachments:
        if a.key == "ATT_S1":
            a.md5 = "TAMPERED"                              # storage association changed
    r = verify_merge(snap, p)
    assert r.passed is False and "attachment-storage-integrity" in names(r)


def test_inv10b_storage_filename_drift(snap):
    p = project(snap)
    for a in p.attachments:
        if a.key == "ATT_M":
            a.filename = "renamed.pdf"                      # filename half of (md5, filename)
    r = verify_merge(snap, p)
    assert r.passed is False and "attachment-storage-integrity" in names(r)


def test_inv11_citekey_preservation(snap):
    p = project(snap)
    p.items[MASTER].citekey = "differentKey2099"           # master's pinned BBT citekey changed
    r = verify_merge(snap, p)
    assert r.passed is False and "citekey-preservation" in names(r)


# ════════════════════════════════════════════════════════════════════════════════
#  PART 2 — ADVERSARIAL PROBES (try to make a corrupt observed the gate PASSES)
# ════════════════════════════════════════════════════════════════════════════════

# ── Probe (g): smart_fill must NOT overwrite a NON-empty master field (caught by #3) ──
def test_probe_g_smartfill_cannot_overwrite_nonempty_master(snap):
    """smart_fill=True must only fill snapshot-EMPTY master fields. Here `title` is non-empty in
    the snapshot, so if anything (a buggy projection or a corrupt observed) sets it from a
    secondary, #3 must catch it. We corrupt observed under smart_fill and assert it is caught."""
    p = project(snap, smart_fill=True)
    p.items[MASTER].fields["title"] = "Teotihuacan (dup A)"   # secondary's title clobbering survivor
    r = verify_merge(snap, p, smart_fill=True)
    assert r.passed is False and "master-scalar-preservation" in names(r)


# ── Probe (b): a collection ADDED that was in NO snapshot item — does == catch additions? ──
def test_probe_b_added_collection_caught_by_equality(snap):
    """`==` (not superset) must reject a spurious collection that no cluster member had."""
    p = project(snap)
    p.items[MASTER].collections = sorted(set(p.items[MASTER].collections) | {"COL_GHOST"})
    r = verify_merge(snap, p)
    assert r.passed is False and "collections-equality" in names(r)


# ── Probe (d): a child reparented to a SECONDARY (not the master) — caught by #7? ──
def test_probe_d_child_reparented_to_secondary_caught(snap):
    """Child completeness requires parent == MASTER. Re-parenting to a *secondary* must fail."""
    p = project(snap)
    for a in p.attachments:
        if a.key == "ATT_S1":
            a.parent_key = SEC1                              # left on the secondary, not master
    r = verify_merge(snap, p)
    assert r.passed is False and "child-completeness" in names(r)


# ── Probe (e): an EXTRA note/attachment/annotation — does parity catch ADDITIONS? ──
def test_probe_e_extra_note_caught_by_count_parity(snap):
    """count-parity is ==, so an *added* note (not just a dropped one) must fail."""
    p = project(snap)
    from zotero_write_mcp.merge import NoteSnap
    p.notes.append(NoteSnap(key="N_PHANTOM", version=1, parent_key=MASTER, json={"itemType": "note"}))
    r = verify_merge(snap, p)
    assert r.passed is False and "note-count-parity" in names(r)


def test_probe_e_extra_annotation_caught(snap):
    """An extra annotation on an existing (snapshot) attachment bumps its count → #9 catches it."""
    p = project(snap)
    from zotero_write_mcp.merge import AnnotationSnap
    for a in p.attachments:
        if a.key == "ATT_M":
            a.annotations.append(AnnotationSnap(key="ANN_PHANTOM", version=1, json={}))
    r = verify_merge(snap, p)
    assert r.passed is False and "annotation-parity" in names(r)


# ════════════════════════════════════════════════════════════════════════════════
#  PART 3 — GATE HOLES (xfail: the gate PASSES a genuinely-corrupt observed)
# ════════════════════════════════════════════════════════════════════════════════

def test_former_hole1_tag_typeflip_additive_now_caught(snap):
    """CLOSED (former GATE HOLE #1): an ADDITIVE tag type-flip — keep (aztec,1) AND add (aztec,0) —
    is now rejected by the per-tag type-consistency clause in check #5 (not a bare tuple-superset)."""
    p = project(snap)
    tags = list(p.items[MASTER].tags)
    assert ("aztec", 1) in tags
    tags.append(("aztec", 0))                                # additive type-flip
    p.items[MASTER].tags = tags
    r = verify_merge(snap, p)
    assert r.passed is False and "tags-tuple-superset" in names(r)


def test_former_hole2_dc_replaces_substring_now_caught(snap):
    """CLOSED (former GATE HOLE #2): with SEC1='S1' a substring of SEC2='S1X', dropping S1's own
    dc:replaces URI is now caught — check #6 matches the EXACT trailing item key, not a substring."""
    p = project(snap)
    dc = list(p.items[MASTER].relations.get("dc:replaces", []))
    s1_uri = f"{LIBRARY_BASE}/{SEC1}"
    s2_uri = f"{LIBRARY_BASE}/{SEC2}"
    assert s1_uri in dc and s2_uri in dc
    dc.remove(s1_uri)                                        # S1's OWN link is gone
    assert s2_uri in dc                                      # '.../S1X' must NOT mask the missing S1
    p.items[MASTER].relations["dc:replaces"] = dc
    r = verify_merge(snap, p)
    assert r.passed is False and "relations-superset" in names(r)


@pytest.mark.xfail(
    strict=False,
    reason="GATE HOLE #3 (version-drift on the MASTER is NOT checked): check #2 only iterates "
           "`sec_keys`; the master is excluded BY DESIGN (it legitimately advances via PATCH, with "
           "concurrency enforced at PATCH-time by If-Unmodified-Since-Version — see merge.py:277-279). "
           "So a master whose OBSERVED version differs from snapshot+1 (e.g. a concurrent third-party "
           "edit landed in the merge window) is invisible to verify_merge. Severity: DOCUMENTED "
           "RESIDUAL, not a Phase-1 gate bug — this is the concurrent-edit-in-window risk owned by the "
           "PATCH-time precondition + the commit phase, NOT by the structural gate. Recorded here so "
           "the residual is explicit and the exclusion is intentional, not accidental.",
)
def test_hole3_master_version_drift_unchecked(snap):
    """ADVERSARIAL: bump the OBSERVED master version arbitrarily. The gate does not look at it."""
    p = project(snap)
    p.items[MASTER].version = 999999                        # concurrent edit landed on the master
    r = verify_merge(snap, p)
    # Desired-if-it-were-in-scope behaviour: a drifted master version would FAIL. It does not (by
    # design), so this xfails — documenting the residual rather than asserting a bug.
    assert r.passed is False, (
        "RESIDUAL: master version drift is intentionally not a structural-gate concern"
    )
