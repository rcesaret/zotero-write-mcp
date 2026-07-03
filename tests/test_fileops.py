"""Seed tests for fileops DOI extraction + filename-metadata heuristics (S0 C.6 / PLAN4 W6).

Includes adversarial cases (no-DOI, multi-DOI, non-ASCII filename, too-short DOI, missing file). These
feed the yet-unbuilt Phase-4 zot-ingest pipeline; pinned now so calibration has a behavioural baseline.
Pure/offline — no network, no library."""
from zotero_write_mcp.fileops import (
    extract_doi_from_pdf, extract_metadata_from_filename, get_content_type,
)


def _write(tmp_path, name, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ── extract_doi_from_pdf ──────────────────────────────────────────────────────────────────────────

def test_doi_extracted(tmp_path):
    f = _write(tmp_path, "a.pdf", b"header ... DOI: 10.1073/pnas.2018155118 more text")
    assert extract_doi_from_pdf(f) == "10.1073/pnas.2018155118"


def test_doi_none_when_absent(tmp_path):
    f = _write(tmp_path, "b.pdf", b"no identifier here at all")
    assert extract_doi_from_pdf(f) is None


def test_doi_multi_returns_first(tmp_path):
    f = _write(tmp_path, "c.pdf", b"first 10.1111/aaa.111 then 10.2222/bbb.222")
    assert extract_doi_from_pdf(f) == "10.1111/aaa.111"


def test_doi_trailing_period_stripped(tmp_path):
    f = _write(tmp_path, "d.pdf", b"see 10.1000/xyz123456.")
    assert extract_doi_from_pdf(f) == "10.1000/xyz123456"


def test_doi_too_short_rejected(tmp_path):
    # '10.1234/a' matches the regex but is len 9 (<= 10) -> rejected by the length guard.
    f = _write(tmp_path, "e.pdf", b"ref 10.1234/a end")
    assert extract_doi_from_pdf(f) is None


def test_doi_nonascii_filename(tmp_path):
    # Non-ASCII in the FILE NAME must not break byte reading; the DOI in content is still found.
    f = _write(tmp_path, "Müller_über_studie.pdf", b"doi 10.5555/abcd.efgh here")
    assert extract_doi_from_pdf(f) == "10.5555/abcd.efgh"


def test_doi_missing_file_returns_none():
    assert extract_doi_from_pdf("C:/no/such/file-xyz-nonexistent.pdf") is None


# ── extract_metadata_from_filename ────────────────────────────────────────────────────────────────

def test_filename_author_year_title():
    m = extract_metadata_from_filename("Sanders (1979) Basin of Mexico.pdf")
    assert m["date"] == "1979"
    assert m["authors"] == ["Sanders"]
    assert "Basin of Mexico" in m["title"]


def test_filename_et_al():
    m = extract_metadata_from_filename("Smith et al. (2018) Great Paper.pdf")
    assert m["date"] == "2018"
    assert m["authors"] == ["Smith et al."]


def test_filename_two_authors_ampersand():
    m = extract_metadata_from_filename("Jones & Brown (2019) Something.pdf")
    assert m["date"] == "2019"
    assert m["authors"] == ["Jones", "Brown"]


def test_filename_no_year_fallback_title():
    m = extract_metadata_from_filename("random_scan_notes.pdf")
    assert m["date"] == ""
    assert m["title"] == "random scan notes"       # underscores -> spaces, no author/date parsed


def test_filename_nonascii_author():
    m = extract_metadata_from_filename("Müller (2020) Überblick.pdf")
    assert m["date"] == "2020"
    assert m["authors"] == ["Müller"]
    assert "Überblick" in m["title"]


# ── get_content_type ──────────────────────────────────────────────────────────────────────────────

def test_content_type_known_and_unknown():
    assert get_content_type("x.pdf") == "application/pdf"
    assert get_content_type("x.md") == "text/markdown"
    assert get_content_type("x.unknownext") == "application/octet-stream"
