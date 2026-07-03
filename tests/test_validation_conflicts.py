"""Adversarial identifier-disagreement mini-suite (sprint S3, PLAN1 SS0 exit gate item 2).

Mirrors the Phase-1 merge injection-suite pattern (tests/test_verify_injection.py): one constructed
failure case per conflict invariant, each proven to route to "flag" and NEVER "accept" through the
FULL pipeline (score_record -> apply_calibration -> decide, via build_validation_result) — not just a
synthetic decide() call with a hand-fed Conflict list (that lives in test_validation.py). This is the
load-bearing invariant of the sprint: an identifier disagreement must NEVER be out-scored by a high p.
"""
import pytest

from zotero_write_mcp.validation import build_validation_result, load_calibration

CALIB = load_calibration("nonexistent-cold-start.json")

CANDIDATE = {
    "itemType": "journalArticle",
    "title": "Basin of Mexico Settlement Patterns and Chronology",
    "date": "1979",
    "creators": [{"creatorType": "author", "firstName": "William", "lastName": "Sanders"}],
    "DOI": "10.1234/basin-of-mexico-1979",
    "publicationTitle": "Academic Press",
}


def _authority(**over):
    base = {"source": "crossref", "title": CANDIDATE["title"], "date": "1979",
            "creators": [{"lastName": "Sanders"}], "doi": CANDIDATE["DOI"],
            "container_title": "Academic Press", "item_type": "journalArticle"}
    base.update(over)
    return base


# ============================ case 1: two authorities, two different DOIs ============================

def test_INJECT_two_authorities_two_dois_routes_to_flag_never_accept():
    authorities = [
        _authority(source="crossref", doi="10.1234/basin-of-mexico-1979"),
        _authority(source="openalex", doi="10.9999/a-completely-different-doi"),
    ]
    res = build_validation_result(CANDIDATE, authorities, CALIB)
    assert res["decision"] == "flag"
    assert any(c["kind"] == "id_disagreement" for c in res["conflicts"])


# ============================ case 2: a DOI that fails to resolve anywhere ============================

def test_INJECT_doi_fails_to_resolve_routes_to_flag_never_accept():
    # The candidate carries a DOI; every authority was consulted (doi_lookup_attempted=True) and NONE
    # of them could resolve it — a bad/retracted/mistyped DOI, or a not-yet-indexed one.
    res = build_validation_result(CANDIDATE, [], CALIB, doi_lookup_attempted=True)
    assert res["decision"] == "flag"
    assert any(c["kind"] == "doi_unresolved" for c in res["conflicts"])


# ============================ case 3: item-type mismatch on a near-identical title ============================

def test_INJECT_item_type_mismatch_routes_to_flag_never_accept():
    # A strong-title-match authority disagrees on WHAT KIND of work this is (book vs journalArticle) —
    # likely a different work wearing a similar title, not a confirming source.
    authorities = [_authority(source="crossref", item_type="book")]
    res = build_validation_result(CANDIDATE, authorities, CALIB)
    assert res["decision"] == "flag"
    assert any(c["kind"] == "item_type_mismatch" for c in res["conflicts"])


# ============================ case 4 (DECISIVE): high p WITH a conflict — the override must win ============

def test_INJECT_high_p_with_conflict_the_override_beats_the_score_DECISIVE():
    """THE decisive case (PLAN1 SS0 / this sprint's G.2): a record with excellent field agreement on
    ONE authority (full identifier agreement, p well above the 0.90 accept floor) PLUS a conflicting
    DOI from a SECOND authority. This proves the CONFLICT-OVERRIDE (evaluated FIRST) beats a strong
    score. If this ever returns "accept", the sprint FAILS — do not flip any feature row."""
    authorities = [
        # Authority #1: agrees on EVERYTHING, including the candidate's own DOI -> id_agreement=True,
        # p_raw would be a perfect 1.0 on its own.
        _authority(source="crossref", doi=CANDIDATE["DOI"]),
        # Authority #2: same title/author/year (so it still contributes to a high p_raw) but a
        # DIFFERENT DOI -> the identifier-disagreement conflict.
        _authority(source="openalex", doi="10.5555/a-conflicting-doi-from-a-second-authority"),
    ]
    res = build_validation_result(CANDIDATE, authorities, CALIB)
    assert res["p_raw"] >= 0.90, "fixture must actually produce a high raw score for this to be decisive"
    assert res["decision"] == "flag", (
        f"CONFLICT OVERRIDE FAILED: expected 'flag' despite p_raw={res['p_raw']}, got "
        f"{res['decision']!r} — a conflicting record must NEVER reach accept regardless of score."
    )
    assert res["decision"] != "accept"
    assert any(c["kind"] == "id_disagreement" for c in res["conflicts"])


def test_INJECT_high_p_with_conflict_holds_even_with_a_generous_calibration():
    """Same decisive case, but with a calibration that maps p_raw practically 1:1 to p (i.e. NOT
    relying on the conservative cold-start floor to hide the bug) — proves the override is a
    STRUCTURAL property of decide(), not an artifact of the conservative calibration being too low
    to reach the accept band in the first place."""
    generous_calib = dict(CALIB, platt={"A": 20.0, "B": -1.0})   # saturates near 1.0 for any real p_raw
    authorities = [
        _authority(source="crossref", doi=CANDIDATE["DOI"]),
        _authority(source="openalex", doi="10.5555/a-conflicting-doi-from-a-second-authority"),
    ]
    res = build_validation_result(CANDIDATE, authorities, generous_calib)
    assert res["p"] >= 0.90, "the generous calibration must actually saturate p for this to be decisive"
    assert res["decision"] == "flag"
