"""Seed/characterization tests for the legacy advisory utils (S0 C.6 / PLAN4 W6): parse_bibtex,
compute_duplicate_score, and the Crossref polite-pool User-Agent. Pins current behaviour before Phase 3
replaces the fuzzy path. Pure/offline — no network."""
from zotero_write_mcp import __version__
from zotero_write_mcp.utils import (
    parse_bibtex, compute_duplicate_score, _crossref_user_agent, CROSSREF_MAILTO_DEFAULT,
)


# ── parse_bibtex ──────────────────────────────────────────────────────────────────────────────────

def test_parse_bibtex_article():
    bib = ("@article{key2020, title={A Study of X}, author={Smith, John and Doe, Jane}, "
           "year={2020}, journal={J. Test}, doi={10.1/x}}")
    item = parse_bibtex(bib)
    assert item is not None
    assert item["itemType"] == "journalArticle"
    assert item["title"] == "A Study of X"
    assert item["date"] == "2020"
    assert item["DOI"] == "10.1/x"
    assert item["publicationTitle"] == "J. Test"
    assert len(item["creators"]) == 2
    assert item["creators"][0] == {"creatorType": "author", "lastName": "Smith", "firstName": "John"}


def test_parse_bibtex_book_type_and_nameless_author():
    item = parse_bibtex("@book{k, title={T}, author={Ada Lovelace}, publisher={P}}")
    assert item is not None and item["itemType"] == "book"
    # "Ada Lovelace" (no comma) -> firstName Ada, lastName Lovelace (rsplit on last space)
    assert item["creators"][0]["firstName"] == "Ada"
    assert item["creators"][0]["lastName"] == "Lovelace"


def test_parse_bibtex_empty_and_garbage_return_none():
    assert parse_bibtex("") is None
    assert parse_bibtex("not bibtex at all {{{") is None


def test_parse_bibtex_first_entry_only():
    two = "@article{a, title={First}, year={2001}}\n@article{b, title={Second}, year={2002}}"
    item = parse_bibtex(two)
    assert item is not None and item["title"] == "First"


def test_parse_bibtex_pages_normalized():
    item = parse_bibtex("@article{k, title={T}, pages={12--34}}")
    assert item is not None and item["pages"] == "12-34"


# ── compute_duplicate_score ───────────────────────────────────────────────────────────────────────

def test_dupscore_missing_title_is_zero():
    assert compute_duplicate_score({"title": ""}, {"title": "X"}) == 0.0
    assert compute_duplicate_score({"data": {"title": "X"}}, {"data": {"title": ""}}) == 0.0


def test_dupscore_exact_doi_is_one_case_insensitive():
    a = {"title": "Totally Different A", "DOI": "10.1/ABC"}
    b = {"title": "Totally Different B", "DOI": "10.1/abc"}   # case-insensitive DOI match short-circuits
    assert compute_duplicate_score(a, b) == 1.0


def test_dupscore_identical_title_author_year_is_one():
    a = {"title": "Basin of Mexico", "creators": [{"creatorType": "author", "lastName": "Sanders"}], "date": "1979"}
    b = {"title": "Basin of Mexico", "creators": [{"creatorType": "author", "lastName": "Sanders"}], "date": "1979"}
    assert compute_duplicate_score(a, b) == 1.0            # 0.55 + 0.30 + 0.15


def test_dupscore_year_off_by_one_partial_date_weight():
    a = {"title": "Same Title", "creators": [{"creatorType": "author", "lastName": "X"}], "date": "2000"}
    b = {"title": "Same Title", "creators": [{"creatorType": "author", "lastName": "X"}], "date": "2001"}
    # title 0.55 + author 0.30 + date(off-by-one -> 0.5)*0.15 = 0.925
    assert abs(compute_duplicate_score(a, b) - 0.925) < 0.01


def test_dupscore_bounded_and_low_for_unrelated():
    a = {"title": "Alpha", "date": "1990"}
    b = {"title": "Omega Beta Gamma Delta", "date": "2020"}
    s = compute_duplicate_score(a, b)
    assert 0.0 <= s < 0.5


# ── Crossref polite-pool User-Agent (C.6) ─────────────────────────────────────────────────────────

def test_crossref_user_agent_from_env(monkeypatch):
    monkeypatch.setenv("CROSSREF_MAILTO", "someone@example.org")
    ua = _crossref_user_agent()
    assert ua == f"ZoteroWriteMCP/{__version__} (mailto:someone@example.org)"
    assert "research@example.com" not in ua               # the old placeholder mailto is gone


def test_crossref_user_agent_default_and_real_version(monkeypatch):
    monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
    ua = _crossref_user_agent()
    assert CROSSREF_MAILTO_DEFAULT == "rcesaret@asu.edu"
    assert CROSSREF_MAILTO_DEFAULT in ua and f"ZoteroWriteMCP/{__version__}" in ua
    assert "/0.1 (" not in ua                              # real version, not the '0.1' placeholder
