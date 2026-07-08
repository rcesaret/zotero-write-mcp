"""Offline unit suite for the Phase-4 ingest extractor (sprint S4; TC-8).

Pure-offline: no network, no keys, no live library, no GROBID. Every side effect is faked
(fake transports / authorities / grobid / llm), mirroring tests/test_sources.py and
tests/test_validation.py. The decisive test is test_wrong_llm_header_forces_review — the
INV-COMP proof that agreement_confidence comes from cross-source authority agreement and a
confident-but-wrong LLM header can never reach "accept".
"""
import json

import pytest

from zotero_write_mcp import ingest as I
from zotero_write_mcp.sources import NormalizedRecord
from zotero_write_mcp.validation import ACCEPT_P_FLOOR


# ═══ fakes (mirror tests/test_sources.py) ═══════════════════════════════════════════════════════

class FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class ConstTransport:
    """Returns one scripted response for every call; records calls for assertions."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self._resp


class RaisingTransport:
    """Raises on any call — proves a path never touches the network."""

    def request(self, *a, **k):
        raise AssertionError("network must not be touched")


class FakeGrobid:
    """Injectable Path-A parser double: scripted availability + header."""

    def __init__(self, header=None, up=True):
        self._header = header
        self._up = up
        self.header_calls = []

    def available(self):
        return self._up

    def process_header(self, pdf_path):
        self.header_calls.append(pdf_path)
        return dict(self._header) if self._header else None


class FakeAuthority:
    """Duck-typed sources.Authority double returning one scripted NormalizedRecord (or nothing)."""

    def __init__(self, name, record=None, up=True):
        self.name = name
        self._record = record
        self._up = up
        self.doi_calls = []
        self.search_calls = []

    def available(self):
        return self._up

    def lookup_by_doi(self, doi):
        self.doi_calls.append(doi)
        return self._record

    def search(self, record):
        self.search_calls.append(record)
        return [self._record] if self._record is not None else []


# ═══ canned fixtures (mirror tests/test_validation.py SANDERS) ═══════════════════════════════════

DOI = "10.1234/abc"
TITLE = "Basin of Mexico Settlement Patterns"


def rec(source, **over):
    base = dict(title=TITLE,
                creators=[{"firstName": "William", "lastName": "Sanders"}],
                year="1979", doi=DOI, container_title="Academic Press",
                item_type="journal-article")
    base.update(over)
    return NormalizedRecord(source=source, **base)


CORRECT_HEADER = {
    "itemType": "journalArticle", "title": TITLE,
    "creators": [{"creatorType": "author", "firstName": "William", "lastName": "Sanders"}],
    "date": "1979", "DOI": DOI, "publicationTitle": "Academic Press",
}

_EN_SENTENCE = ("The study of the settlement patterns in the Basin of Mexico shows that the "
                "population grew during the classical period and it was concentrated in the "
                "urban centers. ")
EN_TEXT = _EN_SENTENCE * 20          # ~3300 chars, no form feeds -> 1 page -> ~3300 chars/page

GERMAN_TEXT = ("Die Untersuchung der Siedlungsmuster zeigt, dass die Bevölkerung während der "
               "klassischen Periode stark zunahm und sich vor allem bei den städtischen Zentren "
               "konzentrierte. Diese Entwicklung wurde durch neue Ausgrabungen bestätigt.")

SEED_MD = """---
title: "Basin of Mexico Settlement Patterns"
author: "Sanders, William"
year: 1979
doi: 10.1234/abc
---

# Basin of Mexico Settlement Patterns

```bibtex
@article{sandersBasinMexico1979,
  title = {Basin of Mexico Settlement Patterns},
  author = {Sanders, William},
  journal = {Academic Press},
  year = {1979},
  doi = {10.1234/abc},
}
```

Body text of the corrected markdown.
"""

TEI = """<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title level="a" type="main">Basin of Mexico Settlement Patterns</title></titleStmt>
      <publicationStmt><date type="published" when="1979-06-01"/></publicationStmt>
      <sourceDesc><biblStruct>
        <analytic>
          <author><persName>
            <forename type="first">William</forename><surname>Sanders</surname>
          </persName></author>
          <idno type="DOI">10.1234/abc</idno>
        </analytic>
        <monogr><title level="j">Academic Press</title>
          <imprint><date type="published" when="1979"/></imprint></monogr>
      </biblStruct></sourceDesc>
    </fileDesc>
  </teiHeader>
</TEI>"""


def blocks_mix(text_n, image_n, discarded_n=0):
    out = [{"type": "text", "text": "body", "page_idx": 0} for _ in range(text_n)]
    out += [{"type": "image", "img_path": "images/i.png", "page_idx": 0} for _ in range(image_n)]
    out += [{"type": "header", "text": "running head", "page_idx": 0} for _ in range(discarded_n)]
    return out


def run_extract(tmp_path, *, text=None, is_ocr=None, blocks=None, md_text="",
                lang_hint="en", route_hint="", grobid=None, llm=None, authorities=None):
    """Drive extract_pdf_metadata with tmp fixtures; authorities defaults to [] (NEVER the live
    default_authorities), and calibration is pinned to the in-code cold-start DEFAULT."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 offline fixture")
    kw = {}
    if md_text:
        md = tmp_path / "paper.md"
        md.write_text(md_text, encoding="utf-8")
        kw["md_path"] = str(md)
    if is_ocr is not None:
        rep = tmp_path / "report.json"
        rep.write_text(json.dumps([{
            "source": "./paper.pdf", "state": "done",
            "output_dir": str(tmp_path / "paper_abc123"),
            "markdown": str(tmp_path / "paper_abc123" / "paper.md"),
            "settings": {"model_version": "vlm", "is_ocr": is_ocr, "language": "en"},
        }]), encoding="utf-8")
        kw["mineru_report_path"] = str(rep)
    if blocks is not None:
        cl = tmp_path / "content_list.json"
        cl.write_text(json.dumps(blocks), encoding="utf-8")
        kw["content_list_path"] = str(cl)
    return I.extract_pdf_metadata(
        str(pdf), lang_hint=lang_hint, route_hint=route_hint,
        text_extractor=(lambda p: text) if text is not None else None,
        grobid=grobid, llm=llm,
        authorities=authorities if authorities is not None else [],
        calibration_path=str(tmp_path / "no-such-calibration.json"), **kw)


# ═══ 1. triage: DOI / arXiv / ISBN regexes ═══════════════════════════════════════════════════════

def test_triage_doi_plain_and_normalized():
    ids = I.triage_identifiers("This paper (doi:10.1234/AbC.DeF) is cited.")
    assert ids["doi"] == "10.1234/abc.def"          # trailing ')' stripped, lowercased


def test_triage_doi_url_form_and_trailing_punct():
    ids = I.triage_identifiers("", "See https://doi.org/10.1073/pnas.1900239116. And more.")
    assert ids["doi"] == "10.1073/pnas.1900239116"


def test_triage_doi_keeps_balanced_parens():
    ids = I.triage_identifiers("doi 10.1002/(SICI)1097-4679(200004)56:4 here")
    assert ids["doi"] is not None and ids["doi"].startswith("10.1002/(sici)1097-4679")


def test_triage_first_doi_wins_deterministic_order():
    ids = I.triage_identifiers("first 10.1111/first.1 then", "md has 10.2222/second.2")
    assert ids["doi"] == "10.1111/first.1"


def test_triage_arxiv_new_and_old_styles():
    assert I.triage_identifiers("preprint arXiv:2101.12345v2 today")["arxiv"] == "2101.12345v2"
    assert I.triage_identifiers("preprint arXiv:hep-th/9901001 old")["arxiv"] == "hep-th/9901001"


def test_triage_isbn_13_and_10():
    assert I.triage_identifiers("ISBN 978-0-12-345678-9")["isbn"] == "9780123456789"
    assert I.triage_identifiers("ISBN: 0-306-40615-2")["isbn"] == "0306406152"


def test_triage_no_match_is_all_none():
    ids = I.triage_identifiers("no identifiers in this text at all")
    assert ids == {"doi": None, "arxiv": None, "isbn": None}


# ═══ 2. routing worked cases ═════════════════════════════════════════════════════════════════════

def test_route_born_digital_en_all_signals_agree_path_a(tmp_path):
    r = run_extract(tmp_path, text=EN_TEXT, is_ocr=False, blocks=blocks_mix(19, 1),
                    grobid=FakeGrobid(header=CORRECT_HEADER))
    route = r["route"]
    assert route["source_kind"] == "born_digital"
    assert route["decision"] == "path_a"
    assert route["parse_path"] == "grobid"
    assert route["degraded"] is False
    assert route["signals"]["votes"] == {"chars_per_page": "born_digital",
                                         "mineru_is_ocr": "born_digital",
                                         "block_mix": "born_digital"}


def test_route_scanned_all_signals_agree_path_b(tmp_path):
    r = run_extract(tmp_path, text="x" * 12, is_ocr=True, blocks=blocks_mix(2, 8),
                    md_text=SEED_MD)
    route = r["route"]
    assert route["source_kind"] == "scanned"
    assert route["decision"] == "path_b"
    assert route["degraded"] is False


def test_route_humanities_hint_forces_path_b_even_born_digital_en(tmp_path):
    r = run_extract(tmp_path, text=EN_TEXT, is_ocr=False, blocks=blocks_mix(19, 1),
                    route_hint="humanities", md_text=SEED_MD,
                    grobid=FakeGrobid(header=CORRECT_HEADER))
    route = r["route"]
    assert route["decision"] == "path_b"
    assert route["degraded"] is False
    assert "route_hint:humanities" in route["reasons"]


def test_route_discordant_signals_conservative_scanned(tmp_path):
    # is_ocr says scanned, text density + block mix say born-digital -> disagreement -> scanned.
    r = run_extract(tmp_path, text=EN_TEXT, is_ocr=True, blocks=blocks_mix(19, 1))
    route = r["route"]
    assert route["source_kind"] == "scanned"
    assert route["decision"] == "path_b"
    assert "conservative:voting-signals-disagree" in route["reasons"]


def test_route_fewer_than_two_signals_conservative_scanned(tmp_path):
    r = run_extract(tmp_path, text=EN_TEXT)          # only one voting signal
    route = r["route"]
    assert route["source_kind"] == "scanned"
    assert route["decision"] == "path_b"
    assert "conservative:fewer-than-2-voting-signals" in route["reasons"]


def test_route_born_digital_en_grobid_none_degraded_path_b(tmp_path):
    r = run_extract(tmp_path, text=EN_TEXT, is_ocr=False, blocks=blocks_mix(19, 1),
                    md_text=SEED_MD, grobid=None)
    route = r["route"]
    assert route["decision"] == "path_b"
    assert route["degraded"] is True
    assert "grobid_unavailable" in route["reasons"]


def test_route_non_english_forces_path_b(tmp_path):
    r = run_extract(tmp_path, text=EN_TEXT, is_ocr=False, blocks=blocks_mix(19, 1),
                    lang_hint="de", grobid=FakeGrobid(header=CORRECT_HEADER))
    route = r["route"]
    assert route["lang"] == "de"
    assert route["decision"] == "path_b"


# ═══ 3. THE INV-COMP PROOF — a confident wrong LLM header can never reach accept ═════════════════

def test_wrong_llm_header_forces_review(tmp_path):
    """FakeLLM emits a confident but WRONG header (smuggled confidence 0.99); >=2 authorities
    agree with EACH OTHER on the correct record + DOI. Cross-source field DISAGREEMENT (not the
    LLM's self-report) must drive the verdict: needs_review, p below the accept floor, never
    "accept". If this test ever fails the sprint fails (INV-COMP / FR-ING-3)."""
    wrong = {
        "itemType": "journalArticle",
        "title": "A Totally Different Paper",
        "creators": [{"creatorType": "author", "lastName": "Wrong", "firstName": "A."}],
        "date": "1999",
        "confidence": 0.99,                          # smuggled LLM self-report — must be stripped
    }
    auths = [FakeAuthority("crossref", rec("crossref")),
             FakeAuthority("openalex", rec("openalex"))]
    r = run_extract(tmp_path, llm=lambda md: dict(wrong), authorities=auths)

    assert r["needs_review"] is True
    assert r["agreement_confidence"] < ACCEPT_P_FLOOR
    assert r["decision"] != "accept"
    # the smuggled confidence reached nothing: p_raw is LOW, driven by field disagreement,
    # and no confidence-like key survives into the scored/returned candidate fields.
    assert r["validation"]["p_raw"] < 0.5
    assert r["agreement_confidence"] != 0.99
    assert "confidence" not in r["fields"]
    assert all("confidence" not in k.lower() for k in r["fields"])
    # no DOI on the candidate -> the search leg ran (candidate scored against authorities).
    assert auths[0].search_calls and auths[1].search_calls
    assert r["route"]["parse_path"] == "llm_header"
    assert "path_b_never_auto_create" in r["needs_review_reasons"]


# ═══ 4. confidence-strip: identical candidate ± confidence keys -> identical verdict ═════════════

def test_confidence_keys_stripped_verdict_identical(tmp_path):
    base = dict(CORRECT_HEADER)
    smuggled = dict(CORRECT_HEADER, confidence=0.99, self_confidence=1.0,
                    probability=0.999, certainty="high", score=99)
    r1 = run_extract(tmp_path, llm=lambda md: dict(base),
                     authorities=[FakeAuthority("crossref", rec("crossref")),
                                  FakeAuthority("openalex", rec("openalex"))])
    r2 = run_extract(tmp_path, llm=lambda md: dict(smuggled),
                     authorities=[FakeAuthority("crossref", rec("crossref")),
                                  FakeAuthority("openalex", rec("openalex"))])
    assert r1["agreement_confidence"] == r2["agreement_confidence"]
    assert r1["validation"]["p_raw"] == r2["validation"]["p_raw"]
    assert r1["decision"] == r2["decision"]
    assert r1["conflicts"] == r2["conflicts"]
    assert r1["fields"] == r2["fields"]


def test_strip_confidence_keys_unit():
    d = {"title": "T", "confidence": 0.9, "self_confidence": 1.0, "probability": 0.8,
         "certainty": "high", "score": 5, "p": 0.7, "llm_confidence_score": 1.0, "DOI": "10.1/x"}
    out = I.strip_confidence_keys(d)
    assert out == {"title": "T", "DOI": "10.1/x"}


# ═══ 5. correct Path-A consensus accept (the F.1 auto-create-eligibility fixture) ════════════════

def test_path_a_consensus_accept_not_flagged(tmp_path):
    auths = [FakeAuthority("crossref", rec("crossref")),
             FakeAuthority("openalex", rec("openalex"))]
    r = run_extract(tmp_path, text=EN_TEXT, is_ocr=False, blocks=blocks_mix(19, 1),
                    grobid=FakeGrobid(header=CORRECT_HEADER), authorities=auths)
    assert r["route"]["parse_path"] == "grobid"
    assert r["decision"] == "accept"                 # cold-start consensus floor 0.92 >= 0.90
    assert r["agreement_confidence"] >= ACCEPT_P_FLOOR
    assert r["needs_review"] is False
    assert r["needs_review_reasons"] == []
    assert r["validation"]["consensus"] is True and r["validation"]["id_agreement"] is True
    # DOI known from the grobid header -> the doi leg (not search) gathered authorities.
    assert auths[0].doi_calls == [DOI] and not auths[0].search_calls


# ═══ 6. correct Path-B accept STILL needs review (Path B never auto-creates) ═════════════════════

def test_path_b_seed_accept_still_needs_review(tmp_path):
    auths = [FakeAuthority("crossref", rec("crossref")),
             FakeAuthority("openalex", rec("openalex"))]
    r = run_extract(tmp_path, md_text=SEED_MD, authorities=auths)
    assert r["route"]["parse_path"] == "seed"
    assert r["decision"] == "accept"                 # perfect agreement, gate passes...
    assert r["needs_review"] is True                 # ...but Path B NEVER auto-creates
    assert r["needs_review_reasons"] == ["path_b_never_auto_create"]


# ═══ 7. degraded: born-digital-EN with GROBID absent ═════════════════════════════════════════════

def test_degraded_grobid_absent_flags_with_reason(tmp_path):
    auths = [FakeAuthority("crossref", rec("crossref")),
             FakeAuthority("openalex", rec("openalex"))]
    r = run_extract(tmp_path, text=EN_TEXT, is_ocr=False, blocks=blocks_mix(19, 1),
                    md_text=SEED_MD, grobid=None, authorities=auths)
    assert r["needs_review"] is True
    assert "grobid_unavailable" in r["needs_review_reasons"]
    assert "path_b_never_auto_create" in r["needs_review_reasons"]
    assert r["route"]["degraded"] is True


# ═══ 8. identifier disagreement -> flag (inherited conflict-override) ════════════════════════════

def test_id_disagreement_between_authorities_flags(tmp_path):
    auths = [FakeAuthority("crossref", rec("crossref")),
             FakeAuthority("openalex", rec("openalex", doi="10.9999/zzz"))]
    r = run_extract(tmp_path, md_text=SEED_MD, authorities=auths)
    assert r["decision"] == "flag"
    assert r["needs_review"] is True
    assert "id_disagreement" in r["needs_review_reasons"]
    assert any(c["kind"] == "id_disagreement" for c in r["conflicts"])


# ═══ 9. single-authority answer -> cold start never accepts ══════════════════════════════════════

def test_single_authority_answer_never_accepts(tmp_path):
    auths = [FakeAuthority("crossref", rec("crossref")),
             FakeAuthority("openalex", None),                 # answers nothing
             FakeAuthority("semantic_scholar", None, up=False)]  # unavailable
    r = run_extract(tmp_path, md_text=SEED_MD, authorities=auths)
    assert r["decision"] != "accept"
    assert r["needs_review"] is True
    assert r["agreement_confidence"] < ACCEPT_P_FLOOR
    assert r["validation"]["consensus"] is False
    assert r["validation"]["answered_authorities"] == ["crossref"]


def test_explicit_empty_authorities_never_falls_back_to_live_defaults(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("live default_authorities must not be constructed in unit tests")
    monkeypatch.setattr(I, "default_authorities", _boom)
    r = run_extract(tmp_path, md_text=SEED_MD, authorities=[])
    assert r["needs_review"] is True
    assert r["decision"] != "accept"


# ═══ 10. fields / per_field_source composition ═══════════════════════════════════════════════════

def test_fields_composition_consensus_single_match_and_candidate_fallback(tmp_path):
    # crossref carries the venue; openalex does not -> venue resolves via the SINGLE-authority
    # match branch; title/creators/date/DOI resolve via consensus; bookTitle stays candidate-only.
    auths = [FakeAuthority("crossref", rec("crossref")),
             FakeAuthority("openalex", rec("openalex", container_title="", item_type=""))]
    cand = dict(CORRECT_HEADER)
    cand["title"] = TITLE + ": A Regional Study"     # subtitle drops in normalize_title -> match
    cand["publicationTitle"] = "Academic  Press."    # normalizes to the crossref venue
    cand["bookTitle"] = "Some Edited Volume"         # candidate-only passthrough
    r = run_extract(tmp_path, llm=lambda md: dict(cand), authorities=auths)

    assert r["fields"]["title"] == TITLE             # verbatim from the FIRST agreeing authority
    assert r["per_field_source"]["title"] == "consensus:crossref,openalex"
    assert r["per_field_source"]["DOI"] == "consensus:crossref,openalex"
    assert r["per_field_source"]["date"] == "consensus:crossref,openalex"
    assert r["per_field_source"]["creators"] == "consensus:crossref,openalex"
    assert r["fields"]["publicationTitle"] == "Academic Press"
    assert r["per_field_source"]["publicationTitle"] == "crossref"
    assert r["fields"]["bookTitle"] == "Some Edited Volume"
    assert r["per_field_source"]["bookTitle"] == "llm_header"
    # itemType: only crossref carries one -> candidate's matches it -> single-authority source.
    assert r["fields"]["itemType"] == "journalArticle"
    assert r["per_field_source"]["itemType"] == "crossref"


def test_fields_candidate_fallback_labeled_by_origin(tmp_path):
    # No authority answers -> every field falls back to the candidate, labeled by its origin.
    r = run_extract(tmp_path, md_text=SEED_MD, authorities=[])
    assert r["fields"]["title"] == TITLE
    assert r["per_field_source"]["title"] == "fixer_seed"
    assert r["per_field_source"]["DOI"] == "fixer_seed"   # seed carried its own DOI


def test_triage_doi_fill_labeled_triage(tmp_path):
    md = "---\ntitle: \"Some Untitled Draft\"\nauthor: \"Doe, Jane\"\nyear: 2020\n---\n\n" \
         "Body cites https://doi.org/10.5555/xyz in passing.\n"
    r = run_extract(tmp_path, md_text=md, authorities=[])
    assert r["identifiers"]["doi"] == "10.5555/xyz"
    assert r["fields"]["DOI"] == "10.5555/xyz"
    assert r["per_field_source"]["DOI"] == "triage"


# ═══ 11. content_list.json block-mix reader ══════════════════════════════════════════════════════

def test_block_mix_text_heavy_and_image_heavy(tmp_path):
    p = tmp_path / "cl.json"
    p.write_text(json.dumps(blocks_mix(19, 1)), encoding="utf-8")
    image, text = I.read_block_mix(str(p))
    assert image == pytest.approx(0.05) and text == pytest.approx(0.95)
    p2 = tmp_path / "cl2.json"
    p2.write_text(json.dumps(blocks_mix(2, 8)), encoding="utf-8")
    image2, _text2 = I.read_block_mix(str(p2))
    assert image2 == pytest.approx(0.8)


def test_block_mix_discarded_types_excluded_from_denominator(tmp_path):
    p = tmp_path / "cl.json"
    p.write_text(json.dumps(blocks_mix(1, 1, discarded_n=8)), encoding="utf-8")
    image, text = I.read_block_mix(str(p))
    assert image == pytest.approx(0.5) and text == pytest.approx(0.5)


def test_block_mix_middle_json_shape_cleanly_rejected(tmp_path):
    # Documented behavior: a middle.json-shaped dict (pdf_info) is REJECTED -> None (abstain).
    p = tmp_path / "middle.json"
    p.write_text(json.dumps({"pdf_info": [{"para_blocks": [], "page_idx": 0}]}), encoding="utf-8")
    assert I.read_block_mix(str(p)) is None
    assert I.read_block_mix(str(tmp_path / "missing.json")) is None


# ═══ 12. MinerU --json run-report reader ═════════════════════════════════════════════════════════

def test_mineru_report_list_with_matching_output_dir(tmp_path):
    p = tmp_path / "report.json"
    p.write_text(json.dumps([
        {"source": "./other.pdf", "output_dir": "runtime/x/other_111111",
         "settings": {"is_ocr": True}},
        {"source": "./paper.pdf", "output_dir": "runtime/x/paper_abc123",
         "markdown": "runtime/x/paper_abc123/paper.md", "settings": {"is_ocr": False}},
    ]), encoding="utf-8")
    assert I.read_mineru_is_ocr(str(p), pdf_path="C:/in/paper.pdf") is False
    assert I.read_mineru_is_ocr(str(p), pdf_path="C:/in/other.pdf") is True


def test_mineru_report_single_dict_form(tmp_path):
    p = tmp_path / "report.json"
    p.write_text(json.dumps({"source": "./whatever.pdf", "settings": {"is_ocr": True}}),
                 encoding="utf-8")
    assert I.read_mineru_is_ocr(str(p), pdf_path="C:/in/unrelated.pdf") is True


def test_mineru_report_missing_or_ambiguous_abstains(tmp_path):
    assert I.read_mineru_is_ocr(str(tmp_path / "missing.json"), pdf_path="x.pdf") is None
    p = tmp_path / "report.json"
    p.write_text(json.dumps([
        {"source": "./a.pdf", "settings": {"is_ocr": True}},
        {"source": "./b.pdf", "settings": {"is_ocr": False}},
    ]), encoding="utf-8")
    assert I.read_mineru_is_ocr(str(p), pdf_path="C:/in/zzz.pdf") is None


# ═══ 13. language detector ═══════════════════════════════════════════════════════════════════════

def test_language_detector_en_other_unknown():
    assert I.detect_language(EN_TEXT) == "en"
    assert I.detect_language(GERMAN_TEXT) != "en"
    assert I.detect_language("El estudio de los patrones de asentamiento en la cuenca "
                             "muestra que la población creció durante el periodo.") != "en"
    assert I.detect_language("") == "unknown"


def test_language_hint_overrides_detection(tmp_path):
    r = run_extract(tmp_path, text=GERMAN_TEXT, lang_hint="en", md_text=SEED_MD)
    assert r["route"]["lang"] == "en"


# ═══ 14. GrobidClient (fake transport; never raises) ═════════════════════════════════════════════

def test_grobid_client_parses_tei_header(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    t = ConstTransport(FakeResp(200, text=TEI))
    g = I.GrobidClient(base_url="http://grobid.test:8070", transport=t)
    cand = g.process_header(str(pdf))
    assert cand["title"] == TITLE
    assert cand["creators"] == [{"creatorType": "author", "firstName": "William",
                                 "lastName": "Sanders"}]
    assert cand["date"] == "1979-06-01"
    assert cand["DOI"] == DOI
    assert cand["publicationTitle"] == "Academic Press"
    call = t.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://grobid.test:8070/api/processHeaderDocument"
    assert "files" in call and call["files"]["input"][0] == "paper.pdf"


def test_grobid_client_available_true_false_and_error():
    assert I.GrobidClient(transport=ConstTransport(FakeResp(200, text="true"))).available() is True
    assert I.GrobidClient(transport=ConstTransport(FakeResp(200, text="false"))).available() is False
    assert I.GrobidClient(transport=ConstTransport(FakeResp(503, text="true"))).available() is False
    assert I.GrobidClient(transport=RaisingTransport()).available() is False


def test_grobid_client_process_header_degrades_to_none(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    assert I.GrobidClient(transport=RaisingTransport()).process_header(str(pdf)) is None
    assert I.GrobidClient(
        transport=ConstTransport(FakeResp(500, text="err"))).process_header(str(pdf)) is None
    assert I.GrobidClient(
        transport=ConstTransport(FakeResp(200, text="<not-xml"))).process_header(str(pdf)) is None
    # missing pdf file: still no raise (empty upload), response parse decides
    assert I.GrobidClient(
        transport=ConstTransport(FakeResp(200, text=TEI))).process_header(
            str(tmp_path / "missing.pdf")) is not None


def test_grobid_client_env_default_url(monkeypatch):
    monkeypatch.delenv("GROBID_URL", raising=False)
    assert I.GrobidClient().base_url == "http://localhost:8070"
    monkeypatch.setenv("GROBID_URL", "http://wsl-host:9070/")
    assert I.GrobidClient().base_url == "http://wsl-host:9070"


# ═══ 15. read-only import boundary (mirror test_sources.py's AST guard) ══════════════════════════

def test_module_imports_only_readonly_leaf_modules():
    """ingest.py may import ONLY the read-only leaf engine modules (dedup / sources / validation /
    utils) — never a write surface (gateway, client, merge, merge_live, server, observability,
    safety, provenance, fileops), never sqlite3 or pyzotero."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(I))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name)
    engine_internal = {m for m in imported
                       if m.startswith(".") or m.startswith("zotero_write_mcp")}
    assert engine_internal <= {".dedup", ".sources", ".validation", ".utils"}, engine_internal
    for forbidden in ("sqlite3", "pyzotero"):
        assert forbidden not in imported, forbidden
    for surface in ("gateway", "client", "merge", "merge_live", "server", "observability",
                    "safety", "provenance", "fileops"):
        assert f".{surface}" not in imported
        assert f"zotero_write_mcp.{surface}" not in imported


# ═══ extras: no-candidate path + seed parser ═════════════════════════════════════════════════════

def test_no_candidate_returns_flagged_empty(tmp_path):
    r = run_extract(tmp_path)                        # no md, no llm, no grobid, no signals
    assert r["needs_review"] is True
    assert r["needs_review_reasons"] == ["no_candidate"]
    assert r["fields"] == {} and r["per_field_source"] == {}
    assert r["agreement_confidence"] == 0.0
    assert r["route"]["parse_path"] == "none"


def test_seed_candidate_bibtex_primary_yaml_fills_gaps():
    cand = I._seed_candidate(SEED_MD)
    assert cand["title"] == TITLE
    assert cand["DOI"] == DOI
    assert cand["date"] == "1979"
    assert cand["publicationTitle"] == "Academic Press"
    assert cand["creators"][0]["lastName"] == "Sanders"
    yaml_only = "---\ntitle: \"Only YAML Here\"\nauthor: \"Doe, Jane and Roe, Rick\"\n" \
                "year: 2021\ndoi: 10.1/y\n---\n\nBody.\n"
    cand2 = I._seed_candidate(yaml_only)
    assert cand2["title"] == "Only YAML Here"
    assert cand2["date"] == "2021"
    assert cand2["DOI"] == "10.1/y"
    assert [c["lastName"] for c in cand2["creators"]] == ["Doe", "Roe"]
    assert I._seed_candidate("") is None
    assert I._seed_candidate("plain body, no yaml, no bibtex") is None


def test_llm_none_falls_back_to_seed(tmp_path):
    r = run_extract(tmp_path, md_text=SEED_MD, llm=lambda md: None, authorities=[])
    assert r["route"]["parse_path"] == "seed"
    assert r["fields"]["title"] == TITLE
