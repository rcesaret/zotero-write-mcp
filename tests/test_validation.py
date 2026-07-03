"""Offline tests for the Phase-3 validation scorer + PINNED 3-way gate + calibration (sprint S3).

Pure-offline: no network, no keys, no live library. Fixtures are plain dicts (Zotero-native field
names) standing in for a candidate record and ``NormalizedRecord``-shaped authority answers (the
scorer accepts either a dict or a real ``NormalizedRecord`` — see ``test_accepts_normalized_record``).
"""
import json

import pytest

from zotero_write_mcp import validation as V
from zotero_write_mcp.sources import NormalizedRecord
from zotero_write_mcp.validation import (
    Conflict,
    GateEvidence,
    apply_calibration,
    build_validation_result,
    canonical_identity_string,
    compute_approval_token,
    decide,
    load_calibration,
    normalized_identity,
    score_record,
    verify_approval_token,
)

SANDERS = {"itemType": "journalArticle", "title": "Basin of Mexico Settlement Patterns",
           "date": "1979", "creators": [{"creatorType": "author", "lastName": "Sanders",
                                          "firstName": "William"}],
           "DOI": "10.1234/abc", "publicationTitle": "Academic Press"}


def auth(**over):
    base = {"source": "crossref", "title": "Basin of Mexico Settlement Patterns", "date": "1979",
            "creators": [{"lastName": "Sanders"}], "doi": "10.1234/abc",
            "container_title": "Academic Press", "item_type": "journalArticle"}
    base.update(over)
    return base


# ============================ scorer: per-field agreement ============================

def test_perfect_match_all_fields_full_score():
    r = score_record(SANDERS, [auth()])
    assert r.per_field["title"] == pytest.approx(1.0)
    assert r.per_field["author"] == pytest.approx(1.0)
    assert r.per_field["year"] == pytest.approx(1.0)
    assert r.per_field["venue"] == pytest.approx(1.0)
    assert r.per_field["id"] == pytest.approx(1.0)
    assert r.p_raw == pytest.approx(1.0)
    assert r.id_agreement is True


def test_no_authorities_zero_score_no_crash():
    r = score_record(SANDERS, [])
    assert r.p_raw == 0.0
    assert r.consensus is False
    assert r.id_agreement is False
    assert r.conflicts == []


def test_empty_candidate_never_raises():
    r = score_record({}, [auth()])
    assert r.p_raw == 0.0


def test_year_offby1_partial_credit():
    r = score_record(SANDERS, [auth(date="1980")])
    assert r.per_field["year"] == pytest.approx(0.5)


def test_year_offby2_zero_credit():
    r = score_record(SANDERS, [auth(date="1981")])
    assert r.per_field["year"] == pytest.approx(0.0)


def test_author_mismatch_zero_score():
    r = score_record(SANDERS, [auth(creators=[{"lastName": "Nobody"}])])
    assert r.per_field["author"] == pytest.approx(0.0)


def test_orcid_overlap_is_evidence_only_never_decisive():
    """Candidate + authority share an ORCID but DIFFERENT surnames -> author score stays 0 (ORCID is
    evidence-only, per PLAN1 SS1.4 'never decisive') — only a note is added."""
    cand = dict(SANDERS)
    cand["creators"] = [{"creatorType": "author", "lastName": "Sanders", "orcid": "0000-0001-2345-6789"}]
    a = auth(creators=[{"lastName": "Nobody", "orcid": "0000-0001-2345-6789"}])
    r = score_record(cand, [a])
    assert r.per_field["author"] == pytest.approx(0.0)   # NOT boosted by the ORCID overlap
    assert any("orcid overlap" in n for n in r.evidence)


def test_id_field_no_candidate_doi_no_agreement():
    cand = {k: v for k, v in SANDERS.items() if k != "DOI"}
    r = score_record(cand, [auth()])
    assert r.id_agreement is False
    assert r.per_field["id"] == pytest.approx(0.0)


def test_accepts_normalized_record_authority():
    """The scorer accepts real sources.NormalizedRecord instances, not just dicts."""
    nr = NormalizedRecord(source="crossref", title=SANDERS["title"], creators=[{"lastName": "Sanders"}],
                           year="1979", doi="10.1234/abc", container_title="Academic Press",
                           item_type="journalArticle")
    r = score_record(SANDERS, [nr])
    assert r.p_raw == pytest.approx(1.0)


# ============================ scorer: consensus + conflicts ============================

def test_consensus_requires_two_distinct_authorities_agreeing():
    r1 = score_record(SANDERS, [auth(source="crossref")])
    assert r1.consensus is False
    r2 = score_record(SANDERS, [auth(source="crossref"), auth(source="openalex")])
    assert r2.consensus is True
    assert r2.consensus_count == 2


def test_consensus_independent_of_candidates_own_doi():
    """Two authorities agree with EACH OTHER even when the candidate carries no DOI at all (the
    ingest use case, PLAN1 SS1.4)."""
    cand = {k: v for k, v in SANDERS.items() if k != "DOI"}
    r = score_record(cand, [auth(source="crossref"), auth(source="openalex")])
    assert r.consensus is True
    assert r.id_agreement is False   # candidate had no DOI to agree with


def test_id_disagreement_two_authorities_two_dois():
    r = score_record(SANDERS, [auth(source="crossref", doi="10.1/aaa"),
                               auth(source="openalex", doi="10.1/bbb")])
    kinds = [c.kind for c in r.conflicts]
    assert "id_disagreement" in kinds


def test_id_disagreement_candidate_vs_authority():
    r = score_record(SANDERS, [auth(source="crossref", doi="10.9/different")])
    kinds = [c.kind for c in r.conflicts]
    assert "id_disagreement" in kinds


def test_no_id_disagreement_when_all_agree():
    r = score_record(SANDERS, [auth(source="crossref"), auth(source="openalex")])
    assert not any(c.kind == "id_disagreement" for c in r.conflicts)


def test_doi_unresolved_when_lookup_attempted_and_nothing_returned():
    r = score_record(SANDERS, [], doi_lookup_attempted=True)
    assert any(c.kind == "doi_unresolved" for c in r.conflicts)


def test_no_doi_unresolved_when_lookup_not_attempted():
    r = score_record(SANDERS, [])
    assert not any(c.kind == "doi_unresolved" for c in r.conflicts)


def test_no_doi_unresolved_when_candidate_has_no_doi():
    cand = {k: v for k, v in SANDERS.items() if k != "DOI"}
    r = score_record(cand, [], doi_lookup_attempted=True)
    assert not any(c.kind == "doi_unresolved" for c in r.conflicts)


def test_item_type_mismatch_on_strong_title_match():
    r = score_record(SANDERS, [auth(item_type="book")])
    assert any(c.kind == "item_type_mismatch" for c in r.conflicts)


def test_no_item_type_mismatch_when_titles_differ():
    r = score_record(SANDERS, [auth(item_type="book", title="Something Completely Unrelated")])
    assert not any(c.kind == "item_type_mismatch" for c in r.conflicts)


def test_no_item_type_mismatch_same_family():
    r = score_record(SANDERS, [auth(item_type="article-journal")])   # same family as journalArticle
    assert not any(c.kind == "item_type_mismatch" for c in r.conflicts)


# ============================ the PINNED 3-way gate ============================

def test_gate_accept_requires_floor_and_id_or_consensus_and_no_conflicts():
    ge = GateEvidence(id_agreement=True, consensus=False)
    assert decide(0.95, ge, []) == "accept"


def test_gate_accept_via_consensus_without_id_agreement():
    ge = GateEvidence(id_agreement=False, consensus=True)
    assert decide(0.95, ge, []) == "accept"


def test_gate_high_p_without_id_or_consensus_never_accepts():
    """The AND-clause is a hard structural gate — p alone, even p=0.99, must NOT accept."""
    ge = GateEvidence(id_agreement=False, consensus=False)
    assert decide(0.99, ge, []) == "flag"


def test_gate_reject_low_p_no_id_agreement_no_conflicts():
    ge = GateEvidence(id_agreement=False, consensus=False)
    assert decide(0.10, ge, []) == "reject"


def test_gate_midband_flags():
    ge = GateEvidence(id_agreement=False, consensus=False)
    assert decide(0.70, ge, []) == "flag"


def test_gate_any_conflict_blocks_accept_even_with_id_agreement():
    ge = GateEvidence(id_agreement=True, consensus=False)
    conflicts = [Conflict(kind="item_type_mismatch", detail="x")]
    assert decide(0.95, ge, conflicts) == "flag"


def test_gate_any_conflict_blocks_reject_too():
    """A conflicting-but-low-p record still needs human eyes, not a silent reject."""
    ge = GateEvidence(id_agreement=False, consensus=False)
    conflicts = [Conflict(kind="doi_unresolved", detail="x")]
    assert decide(0.10, ge, conflicts) == "flag"


def test_gate_conflict_override_beats_high_p_DECISIVE():
    """THE decisive invariant (PLAN1 SS0 exit gate / this sprint's G.2): p=0.99, well above the 0.90
    accept floor, WITH full identifier agreement on one authority, WITH a conflicting DOI from a
    second authority -> "flag", never "accept". If this ever returns "accept" the sprint FAILS."""
    conflicts = [Conflict(kind="id_disagreement", detail="two DOIs",
                          values=["10.1/aaa", "10.1/bbb"], authorities=["crossref", "openalex"])]
    ge = GateEvidence(id_agreement=True, consensus=False)
    assert decide(p=0.99, evidence=ge, conflicts=conflicts) == "flag"


def test_gate_dict_shaped_conflicts_also_trigger_override():
    """decide() must recognize a plain-dict conflict (not just a Conflict dataclass instance)."""
    conflicts = [{"kind": "id_disagreement", "detail": "x"}]
    ge = GateEvidence(id_agreement=True, consensus=True)
    assert decide(0.99, ge, conflicts) == "flag"


# ============================ calibration ============================

def test_load_calibration_missing_file_falls_back_to_default(tmp_path):
    c = load_calibration(tmp_path / "does-not-exist.json")
    assert c["calibration_version"] == "cold-start-v1"
    assert c["n_labeled"] == 0


def test_load_calibration_malformed_json_falls_back_to_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    c = load_calibration(p)
    assert c["calibration_version"] == "cold-start-v1"


def test_load_calibration_missing_platt_key_falls_back(tmp_path):
    p = tmp_path / "no-platt.json"
    p.write_text(json.dumps({"n_labeled": 5}), encoding="utf-8")
    c = load_calibration(p)
    assert c["calibration_version"] == "cold-start-v1"


def test_cold_start_perfect_match_without_consensus_stays_below_accept_floor():
    calib = load_calibration("nonexistent-anywhere.json")
    p = apply_calibration(1.0, consensus=False, calibration=calib)
    assert p < V.ACCEPT_P_FLOOR


def test_cold_start_consensus_with_decent_p_raw_crosses_floor():
    calib = load_calibration("nonexistent-anywhere.json")
    p = apply_calibration(0.9, consensus=True, calibration=calib)
    assert p >= V.ACCEPT_P_FLOOR


def test_cold_start_consensus_with_weak_p_raw_stays_below_floor():
    """consensus alone, with poor field agreement, must NOT bypass the floor (consensus_min_p_raw)."""
    calib = load_calibration("nonexistent-anywhere.json")
    p = apply_calibration(0.1, consensus=True, calibration=calib)
    assert p < V.ACCEPT_P_FLOOR


def test_apply_calibration_never_raises_on_extreme_inputs():
    calib = load_calibration("nonexistent-anywhere.json")
    assert 0.0 <= apply_calibration(1e6, False, calib) <= 1.0
    assert 0.0 <= apply_calibration(-1e6, False, calib) <= 1.0


# ============================ build_validation_result (the tool's shape, TC-7) ============================

def test_build_validation_result_shape():
    calib = load_calibration("nonexistent-anywhere.json")
    res = build_validation_result(SANDERS, [auth(source="crossref"), auth(source="openalex")], calib)
    assert set(res.keys()) >= {"p", "decision", "evidence", "conflicts"}
    assert res["decision"] in ("accept", "flag", "reject")
    assert isinstance(res["evidence"], list)
    assert isinstance(res["conflicts"], list)


def test_build_validation_result_conflicts_are_json_serializable():
    calib = load_calibration("nonexistent-anywhere.json")
    res = build_validation_result(SANDERS, [auth(source="crossref", doi="10.1/aaa"),
                                            auth(source="openalex", doi="10.1/bbb")], calib)
    json.dumps(res)   # must not raise
    assert res["decision"] == "flag"


def test_build_validation_result_never_crashes_on_empty_authorities():
    calib = load_calibration("nonexistent-anywhere.json")
    res = build_validation_result(SANDERS, [], calib)
    assert res["decision"] == "flag"   # not enough evidence either way -> fail-toward-flag


# ============================ identity + HMAC approval token ============================

def test_normalized_identity_fixed_order_and_normalization():
    ident = normalized_identity(SANDERS)
    assert ident == ("journalarticle", "basin of mexico settlement patterns", "1979", "sanders",
                     "10.1234/abc")


def test_canonical_identity_string_uses_pipe_delimiter():
    s = canonical_identity_string(SANDERS)
    assert s == "journalarticle|basin of mexico settlement patterns|1979|sanders|10.1234/abc"


def test_canonical_identity_string_never_raises_on_missing_fields():
    assert canonical_identity_string({}) == "||||"


def test_token_round_trip():
    key = b"super-secret-not-committed"
    token = compute_approval_token(SANDERS, key)
    assert verify_approval_token(SANDERS, token, key) is True


def test_token_deterministic_same_record_same_key():
    key = b"k"
    assert compute_approval_token(SANDERS, key) == compute_approval_token(SANDERS, key)


def test_token_differs_across_records():
    key = b"k"
    other = dict(SANDERS, title="A Completely Different Title")
    assert compute_approval_token(SANDERS, key) != compute_approval_token(other, key)


def test_verify_rejects_wrong_key():
    token = compute_approval_token(SANDERS, b"real-key")
    assert verify_approval_token(SANDERS, token, b"wrong-key") is False


def test_verify_rejects_absent_token():
    assert verify_approval_token(SANDERS, None, b"k") is False
    assert verify_approval_token(SANDERS, "", b"k") is False


def test_verify_rejects_absent_key():
    token = compute_approval_token(SANDERS, b"k")
    assert verify_approval_token(SANDERS, token, None) is False


def test_verify_rejects_token_bound_to_a_different_record():
    """A token minted for record X presented against record Y must be rejected — the HMAC is over the
    payload identity, so it must not transfer between records."""
    key = b"k"
    token_for_sanders = compute_approval_token(SANDERS, key)
    other_record = dict(SANDERS, title="A Totally Different Paper", DOI="10.9/zzz")
    assert verify_approval_token(other_record, token_for_sanders, key) is False


def test_verify_never_raises_on_garbage_token():
    assert verify_approval_token(SANDERS, "not-hex-garbage!!", b"k") is False
    assert verify_approval_token(SANDERS, 12345, b"k") is False
