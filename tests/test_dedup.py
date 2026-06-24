"""Unit tests for dedup_scan (P2-dedup-scan) — deterministic ASySD auto-accept + frozen normalizer."""
import pytest

from zotero_write_mcp.dedup import (
    normalize_title, normalize_year, first_author_surname, normalize_doi, asysd_key,
    select_master, dedup_scan,
)


def _item(key, item_type="journalArticle", **data):
    return {"key": key, "version": 1, "data": {"key": key, "itemType": item_type, **data}}


# ── normalizer (FROZEN; H-2) ────────────────────────────────────────────────────

def test_normalize_title_case_punct_diacritics():
    assert normalize_title("The Aztécs, Empire!") == "the aztecs empire"


def test_doi_reuse_across_unrelated_works_demotes():
    """H-3: same DOI but token-DISJOINT titles (DOI reused across unrelated works) -> demote."""
    from zotero_write_mcp.dedup import dedup_scan as _scan
    items = [_item("A", DOI="10.1/x", title="Aztec Empire", date="1979"),
             _item("B", DOI="10.1/x", title="Roman History", date="1979")]
    c = _scan(items)["candidate_clusters"][0]
    assert not c.auto_accept and "gross title disagreement" in c.conflicts


def test_normalize_title_drops_subtitle():
    assert normalize_title("Teotihuacan: An Experimental City") == "teotihuacan"


def test_normalize_year_from_date():
    assert normalize_year(_item("X", date="1979-05-01")) == "1979"
    assert normalize_year(_item("X")) == ""


def test_first_author_surname():
    it = _item("X", creators=[{"creatorType": "author", "lastName": "Sänders"},
                              {"creatorType": "author", "lastName": "Parsons"}])
    assert first_author_surname(it) == "sanders"


def test_normalize_doi_strips_url():
    assert normalize_doi(_item("X", DOI="https://doi.org/10.1234/ABC")) == "10.1234/abc"
    assert normalize_doi(_item("X")) is None


def test_asysd_key_requires_title_and_year():
    assert asysd_key(_item("X", title="T", date="1979",
                           creators=[{"lastName": "Sanders"}])) == ("t", "1979", "sanders")
    assert asysd_key(_item("X", title="T")) is None        # no year -> no deterministic key


# ── master selection (H-6) ──────────────────────────────────────────────────────

def test_select_master_most_complete_then_lowest_key():
    by_key = {
        "B": _item("B", title="t", date="1979"),
        "A": _item("A", title="t", date="1979", abstractNote="rich", publisher="x"),
        "C": _item("C", title="t", date="1979"),
    }
    assert select_master(by_key, ["B", "A", "C"]) == "A"        # most-complete
    # tiebreak: B and C equally complete -> lowest key
    assert select_master(by_key, ["C", "B"]) == "B"


# ── deterministic auto-accept ───────────────────────────────────────────────────

def test_exact_doi_auto_accepts():
    items = [_item("A", title="Paper", date="1979", DOI="10.1/x"),
             _item("B", title="Paper (reprint)", date="1979", DOI="10.1/x")]
    res = dedup_scan(items)
    assert res["auto_accept_count"] == 1
    c = res["candidate_clusters"][0]
    assert c.auto_accept and set(c.item_keys) == {"A", "B"} and c.master_key in {"A", "B"}


def test_doi_conflict_item_type_demotes():
    items = [_item("A", item_type="journalArticle", DOI="10.1/x", title="P", date="1979"),
             _item("B", item_type="book", DOI="10.1/x", title="P", date="1979")]
    res = dedup_scan(items)
    c = res["candidate_clusters"][0]
    assert not c.auto_accept and "item-type conflict" in c.conflicts


def test_normalized_key_auto_accepts():
    items = [_item("A", title="Basin of Mexico", date="1979", creators=[{"lastName": "Sanders"}]),
             _item("B", title="Basin of Mexico: A Study", date="1979", creators=[{"lastName": "Sanders"}])]
    res = dedup_scan(items)
    c = res["candidate_clusters"][0]
    assert c.auto_accept and set(c.item_keys) == {"A", "B"}     # subtitle dropped -> same key


def test_normalized_key_doi_disagreement_demotes():
    """H-2: same normalized title+year+author but DIFFERENT DOIs -> different works -> NOT auto-accept."""
    items = [_item("A", title="Basin of Mexico", date="1979", creators=[{"lastName": "Sanders"}], DOI="10.1/a"),
             _item("B", title="Basin of Mexico", date="1979", creators=[{"lastName": "Sanders"}], DOI="10.1/b")]
    res = dedup_scan(items)
    # different DOIs -> these are in separate DOI singletons, not clustered together at all
    assert res["auto_accept_count"] == 0


def test_normalized_key_doi_disagreement_within_group_demotes():
    """Two share a normalized key; a third path: same normalized key, one has a conflicting DOI."""
    items = [_item("A", title="X Study", date="1980", creators=[{"lastName": "Lee"}], DOI="10.9/a"),
             _item("B", title="X Study", date="1980", creators=[{"lastName": "Lee"}], DOI="10.9/b")]
    # both have DOIs but different -> they land in DOI singletons (len<2) -> no cluster -> 0 auto
    res = dedup_scan(items)
    assert res["auto_accept_count"] == 0


def test_different_item_types_never_auto_merge():
    items = [_item("A", item_type="journalArticle", title="T", date="1979", creators=[{"lastName": "Z"}]),
             _item("B", item_type="conferencePaper", title="T", date="1979", creators=[{"lastName": "Z"}])]
    res = dedup_scan(items)
    c = res["candidate_clusters"][0]
    assert not c.auto_accept and "item-type conflict" in c.conflicts


def test_singletons_no_cluster():
    items = [_item("A", title="Alpha", date="1979"), _item("B", title="Beta", date="1980")]
    res = dedup_scan(items)
    assert res["candidate_clusters"] == [] and res["auto_accept_count"] == 0


def test_probabilistic_seam_disabled():
    res = dedup_scan([], review_threshold=0.95)
    assert res["probabilistic_review"]["enabled"] is False
    assert res["probabilistic_review"]["review_threshold"] == 0.95
