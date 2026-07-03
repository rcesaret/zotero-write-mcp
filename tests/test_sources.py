"""Offline tests for the Phase-3 validation source clients (sprint S-V1).

Pure-offline, mirroring ``tests/test_gateway.py``: a fake transport scripts authority responses and a
fake clock drives the rate governor, so every path is deterministic with NO network and NO keys. The
LIVE smoke against real authorities lives in ``scripts/sources_live_smoke.py`` (run manually) so this
suite never touches the network.

Covers the S-V1 exit-gate OFFLINE checks:
  1. six-adapter normalization on valid / empty / malformed payloads, never raising
  2. rate governor waits out Backoff + retries on 429 honoring Retry-After under a fake clock
  3. cache idempotency + offline re-runnability (a raising-stub transport proves the 2nd hit is cached)
  4. Crossref raw ``score`` orders candidates only, never crosses a threshold
  5. degraded path: OpenAlex self-skips without a key; composition surfaces "openalex: unavailable"
"""
import pytest

from zotero_write_mcp import sources as S
from zotero_write_mcp.sources import (
    CrossrefAdapter,
    DataCiteAdapter,
    DoiNegotiationAdapter,
    JsonCache,
    NormalizedRecord,
    OpenAlexAdapter,
    OrcidAdapter,
    RateGovernor,
    SemanticScholarAdapter,
    default_authorities,
    gather_by_doi,
    gather_by_search,
)
from zotero_write_mcp.dedup import normalize_doi, normalize_title


# ── fakes (mirror tests/test_gateway.py) ────────────────────────────────────────

class FakeResp:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class ConstTransport:
    """Returns one scripted response for every call; records calls for assertions."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def request(self, method, url, *, headers=None, params=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params})
        return self._resp


class SeqTransport:
    """Returns scripted responses in order (for the governor); raises if it runs out."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, params=None):
        self.calls.append({"method": method, "url": url})
        if not self._responses:
            raise AssertionError(f"SeqTransport: no scripted response for {method} {url}")
        return self._responses.pop(0)


class RaisingTransport:
    """Raises on any call — proves a path was served from cache / is evidence-only (no network)."""

    def request(self, *a, **k):
        raise AssertionError("network must not be touched (served from cache / evidence-only)")


class FakeClock:
    """Monotonic clock that only advances when ``sleep`` is called — zero real waiting."""

    def __init__(self, t0=1000.0):
        self.t = t0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.sleeps.append(dt)
        self.t += dt


# ── canned payloads (representative real-authority shapes) ───────────────────────

_TITLE = "Greater post-Neolithic wealth disparities in Eurasia than in North America and Mesoamerica"

CSL_VALID = {
    "title": _TITLE,
    "author": [{"family": "Kohler", "given": "Timothy A."},
               {"family": "Smith", "given": "Michael E."}],
    "issued": {"date-parts": [[2017, 11, 15]]},
    "DOI": "10.1038/nature24646",
    "container-title": "Nature",
    "type": "article-journal",
}

CROSSREF_WORK = {"message": {
    "title": [_TITLE],
    "author": [{"family": "Kohler", "given": "Timothy A."}],
    "issued": {"date-parts": [[2017]]},
    "DOI": "10.1038/NATURE24646",          # upper-case -> tests dedup.normalize_doi reuse
    "container-title": ["Nature"],
    "type": "journal-article",
}}

CROSSREF_SEARCH = {"message": {"items": [
    {"title": ["Candidate B (low score)"], "DOI": "10.2/b", "score": 0.001,
     "issued": {"date-parts": [[2002]]}, "author": [{"family": "B"}], "type": "journal-article"},
    {"title": ["Candidate A (high score)"], "DOI": "10.1/a", "score": 90.5,
     "issued": {"date-parts": [[2001]]}, "author": [{"family": "A"}], "type": "journal-article"},
    {"title": ["Candidate C (no score field)"], "DOI": "10.3/c",
     "issued": {"date-parts": [[2003]]}, "author": [{"family": "C"}], "type": "journal-article"},
]}}

OPENALEX_WORK = {
    "title": _TITLE,
    "authorships": [{"author": {"display_name": "Timothy A. Kohler",
                                "orcid": "https://orcid.org/0000-0002-1825-0097"}}],
    "publication_year": 2017,
    "doi": "https://doi.org/10.1038/nature24646",
    "primary_location": {"source": {"display_name": "Nature"}},
    "type": "article",
    "ids": {"openalex": "https://openalex.org/W2766821303",
            "pmid": "https://pubmed.ncbi.nlm.nih.gov/29167577",
            "doi": "https://doi.org/10.1038/nature24646"},
}

S2_MATCH = {"data": [{
    "title": _TITLE,
    "year": 2017,
    "authors": [{"name": "Timothy A. Kohler"}, {"name": "Michael E. Smith"}],
    "externalIds": {"DOI": "10.1038/nature24646", "ArXiv": None, "PubMed": "29167577"},
    "venue": "Nature",
    "publicationTypes": ["JournalArticle"],
}]}

DATACITE_DOI = {"data": {"attributes": {
    "titles": [{"title": "Basin of Mexico Settlement Survey Dataset"}],
    "creators": [{"givenName": "Jeffrey", "familyName": "Parsons"}, {"name": "INAH"}],
    "publicationYear": 2019,
    "doi": "10.5281/ZENODO.123456",
    "publisher": "Zenodo",
    "types": {"resourceTypeGeneral": "Dataset"},
}}}

ORCID_PERSON = {"name": {"given-names": {"value": "Timothy"}, "family-name": {"value": "Kohler"}}}

DOI = "10.1038/nature24646"


# ═══ Check 1: six-adapter normalization (valid / empty / malformed, never raising) ═══

def test_doi_negotiation_valid():
    a = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(200, body=CSL_VALID)))
    r = a.lookup_by_doi(DOI)
    assert isinstance(r, NormalizedRecord)
    assert r.source == "doi_negotiation"
    assert r.title == _TITLE
    assert r.year == "2017"
    assert r.doi == "10.1038/nature24646"
    assert r.container_title == "Nature"
    assert r.item_type == "article-journal"
    assert {"firstName": "Timothy A.", "lastName": "Kohler"} in r.creators
    assert r.external_ids.get("doi") == "10.1038/nature24646"


def test_doi_negotiation_empty_returns_none():
    a = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(200, body={})))
    assert a.lookup_by_doi(DOI) is None


def test_doi_negotiation_malformed_never_raises():
    a = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(200, body={"author": 5, "title": None})))
    assert a.lookup_by_doi(DOI) is None  # no usable identity -> None, and no exception


def test_doi_negotiation_404_returns_none():
    a = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(404)))
    assert a.lookup_by_doi(DOI) is None


def test_crossref_lookup_valid_normalizes_and_reuses_dedup_doi():
    a = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_WORK)))
    r = a.lookup_by_doi(DOI)
    assert r.source == "crossref"
    assert r.title == _TITLE
    assert r.year == "2017"
    assert r.doi == "10.1038/nature24646"          # dedup.normalize_doi lower-cased the payload's DOI
    assert r.container_title == "Nature"


def test_crossref_lookup_malformed_returns_none():
    a = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body={"no": "message"})))
    assert a.lookup_by_doi(DOI) is None


def test_crossref_search_valid_returns_candidates():
    a = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_SEARCH)))
    cands = a.search({"title": "wealth disparities", "creators": [{"lastName": "Kohler"}]})
    assert len(cands) == 3
    assert all(isinstance(c, NormalizedRecord) for c in cands)


def test_openalex_lookup_valid():
    a = OpenAlexAdapter(transport=ConstTransport(FakeResp(200, body=OPENALEX_WORK)), api_key="k")
    r = a.lookup_by_doi(DOI)
    assert r.source == "openalex"
    assert r.title == _TITLE
    assert r.year == "2017"
    assert r.doi == "10.1038/nature24646"           # https://doi.org/ prefix stripped by dedup
    assert r.container_title == "Nature"
    assert r.external_ids.get("openalex") == "https://openalex.org/W2766821303"
    assert r.external_ids.get("pmid")
    assert r.creators[0]["orcid"] == "0000-0002-1825-0097"


def test_openalex_empty_returns_none():
    a = OpenAlexAdapter(transport=ConstTransport(FakeResp(200, body={})), api_key="k")
    assert a.lookup_by_doi(DOI) is None


def test_openalex_malformed_never_raises():
    a = OpenAlexAdapter(transport=ConstTransport(FakeResp(200, body={"authorships": "x", "doi": None})),
                        api_key="k")
    assert a.lookup_by_doi(DOI) is None


def test_semantic_scholar_match_valid():
    a = SemanticScholarAdapter(transport=ConstTransport(FakeResp(200, body=S2_MATCH)))
    cands = a.search({"title": _TITLE})
    assert len(cands) == 1
    r = cands[0]
    assert r.source == "semantic_scholar"
    assert r.year == "2017"
    assert r.doi == "10.1038/nature24646"
    assert r.external_ids.get("pmid") == "29167577"
    assert r.container_title == "Nature"


def test_semantic_scholar_match_404_returns_empty():
    a = SemanticScholarAdapter(transport=ConstTransport(FakeResp(404)))
    assert a.search({"title": "nothing matches"}) == []


def test_semantic_scholar_malformed_never_raises():
    a = SemanticScholarAdapter(transport=ConstTransport(FakeResp(200, body={"data": "notalist"})))
    assert a.search({"title": _TITLE}) == []


def test_datacite_lookup_valid():
    a = DataCiteAdapter(transport=ConstTransport(FakeResp(200, body=DATACITE_DOI)))
    r = a.lookup_by_doi("10.5281/zenodo.123456")
    assert r.source == "datacite"
    assert r.title == "Basin of Mexico Settlement Survey Dataset"
    assert r.year == "2019"
    assert r.doi == "10.5281/zenodo.123456"
    assert r.item_type == "Dataset"
    assert {"firstName": "Jeffrey", "lastName": "Parsons"} in r.creators


def test_datacite_malformed_returns_none():
    a = DataCiteAdapter(transport=ConstTransport(FakeResp(200, body={"data": 5})))
    assert a.lookup_by_doi(DOI) is None


def test_orcid_author_ids_network_free():
    a = OrcidAdapter(transport=RaisingTransport())  # must NOT touch the network
    rec = NormalizedRecord(source="x",
                           creators=[{"name": "K", "orcid": "https://orcid.org/0000-0002-1825-0097"}],
                           external_ids={"orcid": "0000-0001-0002-0003"})
    ids = a.author_orcids(rec)
    assert "0000-0002-1825-0097" in ids
    assert "0000-0001-0002-0003" in ids


def test_orcid_lookup_profile_evidence():
    a = OrcidAdapter(transport=ConstTransport(FakeResp(200, body=ORCID_PERSON)))
    ev = a.lookup_orcid("0000-0002-1825-0097")
    assert ev["orcid"] == "0000-0002-1825-0097"
    assert "Kohler" in ev["name"]


def test_orcid_lookup_by_doi_and_search_are_evidence_only():
    a = OrcidAdapter(transport=RaisingTransport())
    assert a.lookup_by_doi(DOI) is None
    assert a.search({"title": _TITLE}) == []


# ═══ Check 2: rate governor under a fake clock (Backoff + 429/Retry-After) ═══

def test_governor_waits_out_backoff_no_real_sleep():
    clock = FakeClock()
    gov = RateGovernor(sleep=clock.sleep, monotonic=clock.monotonic)
    t = SeqTransport([FakeResp(200, headers={"Backoff": "5"}), FakeResp(200)])
    gov.request(t, "GET", "http://host/a")
    assert clock.sleeps == []                 # first call does not wait
    gov.request(t, "GET", "http://host/b")
    assert clock.sleeps == [5.0]              # second call waits out the captured Backoff
    assert len(t.calls) == 2


def test_governor_retries_on_429_honoring_retry_after():
    clock = FakeClock()
    gov = RateGovernor(sleep=clock.sleep, monotonic=clock.monotonic)
    t = SeqTransport([FakeResp(429, headers={"Retry-After": "2"}), FakeResp(200)])
    resp = gov.request(t, "GET", "http://host/a")
    assert resp.status_code == 200
    assert clock.sleeps == [2.0]             # honored Retry-After, zero real wall-clock time
    assert len(t.calls) == 2


def test_governor_gives_up_after_max_429_retries():
    clock = FakeClock()
    gov = RateGovernor(sleep=clock.sleep, monotonic=clock.monotonic, max_429_retries=2)
    t = SeqTransport([FakeResp(429, headers={"Retry-After": "1"}),
                      FakeResp(429, headers={"Retry-After": "1"}),
                      FakeResp(429, headers={"Retry-After": "1"})])
    resp = gov.request(t, "GET", "http://host/a")
    assert resp.status_code == 429            # returns the last 429 rather than looping forever
    assert len(t.calls) == 3


# ═══ Check 3: content-addressed cache (idempotent + offline re-runnable) ═══

def test_cache_idempotent_sharded_and_atomic(tmp_path):
    c = JsonCache(tmp_path)
    k = c.key("src", "doi", "10.1/x")
    c.put(k, {"a": 1})
    p = tmp_path / k[:2] / k                   # sharded <sha256[:2]>/<sha256> layout
    assert p.exists()
    assert c.has(k)
    c.put(k, {"a": 999})                        # idempotent: existing blob is kept, never rewritten
    assert c.get(k) == {"a": 1}
    assert not list(tmp_path.rglob("*.tmp-*"))  # atomic: no leftover temp files


def test_cache_offline_rerun_served_without_network(tmp_path):
    cache = JsonCache(tmp_path)
    a1 = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(200, body=CSL_VALID)), cache=cache)
    r1 = a1.lookup_by_doi(DOI)
    assert r1 is not None and r1.title == _TITLE
    assert len(a1._t.calls) == 1
    k = cache.key("doi_negotiation", "doi", normalize_doi({"DOI": DOI}))
    assert (tmp_path / k[:2] / k).exists()
    # a fresh adapter sharing the SAME cache with a raising transport must be served from disk
    a2 = DoiNegotiationAdapter(transport=RaisingTransport(), cache=cache)
    r2 = a2.lookup_by_doi(DOI)                  # would raise if the network were touched
    assert r2 is not None and r2.title == r1.title


def test_cache_miss_sentinel_keeps_404_offline(tmp_path):
    cache = JsonCache(tmp_path)
    a1 = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(404)), cache=cache)
    assert a1.lookup_by_doi(DOI) is None
    a2 = DoiNegotiationAdapter(transport=RaisingTransport(), cache=cache)
    assert a2.lookup_by_doi(DOI) is None        # the cached miss keeps the re-run offline


# ═══ Check 4: Crossref raw `score` orders candidates only — NEVER thresholded ═══

def test_crossref_score_orders_candidates_only():
    a = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_SEARCH)))
    cands = a.search({"title": "x"})
    titles = [c.title for c in cands]
    # ordered by score DESC (90.5, then 0.001, then the no-score candidate at 0.0) — ordering only
    assert titles == ["Candidate A (high score)", "Candidate B (low score)", "Candidate C (no score field)"]


def test_crossref_low_and_missing_score_candidates_not_dropped():
    a = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_SEARCH)))
    cands = a.search({"title": "x"})
    # NO candidate is filtered out by score: the 0.001 and the score-less candidate both survive
    assert len(cands) == 3
    assert any(c.doi == "10.2/b" for c in cands)   # tiny-score candidate present
    assert any(c.doi == "10.3/c" for c in cands)   # score-less candidate present


def test_normalized_record_carries_no_score_derived_field():
    a = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_SEARCH)))
    r = a.search({"title": "x"})[0]
    assert not hasattr(r, "score")
    assert "score" not in r.as_dict()
    for probe in ("score", "confidence", "accepted", "probability"):
        assert probe not in r.as_dict()
    # the raw payload retains score for audit, but no typed decision field is derived from it
    assert "score" in r.raw


# ═══ Check 5: degraded path (OpenAlex self-skips without a key) ═══

def test_openalex_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    a = OpenAlexAdapter(transport=RaisingTransport(), api_key=None)
    assert a.available() is False
    assert a.lookup_by_doi(DOI) is None        # self-skips before any network call (raising transport)
    assert a.search({"title": _TITLE}) == []


def test_openalex_self_enables_when_key_present():
    a = OpenAlexAdapter(transport=ConstTransport(FakeResp(200, body=OPENALEX_WORK)), api_key="SECRET123")
    assert a.available() is True
    assert a.lookup_by_doi(DOI) is not None     # no code change — presence of the key enables it


def test_gather_by_doi_degrades_over_unavailable_openalex(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    doineg = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(200, body=CSL_VALID)))
    openalex = OpenAlexAdapter(transport=RaisingTransport(), api_key=None)
    res = gather_by_doi(DOI, [doineg, openalex])
    assert any("openalex: unavailable" in e for e in res.evidence)   # honest availability note
    assert "doi_negotiation" in res.answered                          # other authority still returns
    assert "openalex" not in res.available
    assert len(res.records) == 1


def test_gather_never_raises_on_erroring_adapter():
    class _Boom:
        name = "boom"

        def available(self):
            return True

        def lookup_by_doi(self, doi):
            raise RuntimeError("kaboom")

        def search(self, record):
            raise RuntimeError("kaboom")

    res = gather_by_doi(DOI, [_Boom()])
    assert res.records == []
    assert any("boom: error" in e for e in res.evidence)   # a bad authority never aborts the batch


# ═══ Composition, reuse, and read-only-boundary strengthening ═══

def test_gather_by_doi_multiple_authorities_agree():
    a1 = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(200, body=CSL_VALID)))
    a2 = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_WORK)))
    res = gather_by_doi(DOI, [a1, a2])
    assert set(res.answered) == {"doi_negotiation", "crossref"}
    assert len(res.records) == 2
    assert {r.doi for r in res.records} == {"10.1038/nature24646"}     # drift-free normalized DOI
    assert {r.year for r in res.records} == {"2017"}
    assert len({normalize_title(r.title) for r in res.records}) == 1    # agree on normalized title


def test_gather_by_search_composes_candidates():
    a1 = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_SEARCH)))
    a2 = SemanticScholarAdapter(transport=ConstTransport(FakeResp(200, body=S2_MATCH)))
    res = gather_by_search({"title": _TITLE}, [a1, a2])
    assert len(res.records) == 4                    # 3 Crossref candidates + 1 S2 match
    assert set(res.answered) == {"crossref", "semantic_scholar"}


def test_openalex_key_never_appears_in_evidence_or_raw():
    key = "OPENALEX_SECRET_TOKEN_do_not_leak"
    a = OpenAlexAdapter(transport=ConstTransport(FakeResp(200, body=OPENALEX_WORK)), api_key=key)
    res = gather_by_doi(DOI, [a])
    assert key not in str(res.evidence)
    for r in res.records:
        assert key not in str(r.raw)
        assert key not in str(r.as_dict())


def test_default_authorities_are_six_and_share_one_cache(tmp_path):
    auths = default_authorities(cache_root=tmp_path)
    assert [a.name for a in auths] == ["doi_negotiation", "crossref", "openalex",
                                       "semantic_scholar", "datacite", "orcid"]
    assert len({id(a._cache) for a in auths}) == 1   # one shared cache -> one network hit per record


def test_all_adapters_conform_to_authority_protocol(tmp_path):
    for a in default_authorities(cache_root=tmp_path):
        assert isinstance(a, S.Authority)


def test_normalized_record_as_dict_roundtrip():
    r = NormalizedRecord(source="crossref", title="T", year="2020", doi="10.1/x",
                         external_ids={"doi": "10.1/x"})
    d = r.as_dict()
    assert d["source"] == "crossref" and d["doi"] == "10.1/x"
    assert set(d) == {"source", "title", "creators", "year", "doi",
                      "container_title", "item_type", "external_ids", "raw"}


def test_module_imports_only_readonly_leaf_modules():
    """Read-only boundary (S-V1 §E.1): the module may import ONLY the read-only leaf engine modules
    (dedup / provenance / utils) — never a write surface (gateway, client, merge, merge_live, server,
    observability, safety), never sqlite3 or pyzotero. Precise AST check (does not false-trip on
    documentation that merely references the write gateway it mirrors)."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(S))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name)
    engine_internal = {m for m in imported
                       if m.startswith(".") or m.startswith("zotero_write_mcp")}
    assert engine_internal <= {".dedup", ".provenance"}, engine_internal
    for forbidden in ("sqlite3", "pyzotero"):
        assert forbidden not in imported, forbidden
    for surface in ("gateway", "client", "merge", "merge_live", "server", "observability", "safety"):
        assert f".{surface}" not in imported
        assert f"zotero_write_mcp.{surface}" not in imported


def test_module_does_not_top_level_import_httpx_or_utils():
    """Finding 1 regression: the module must not import httpx/bibtexparser at module top level, nor
    import the httpx-laden .utils module — the only httpx reference is the LAZY import inside
    HttpxReadTransport.request. A substring grep would false-trip on the docstring that explains this,
    so inspect the actual top-level import statements via AST (nested function imports are excluded)."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(S))
    names = set()
    for n in tree.body:                                # module-level statements only
        if isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            names.add(n.module or "")
    assert "httpx" not in names
    assert "bibtexparser" not in names
    assert ".utils" not in names and "zotero_write_mcp.utils" not in names
    assert S.CROSSREF_BASE == "https://api.crossref.org"


# ═══ Fix 3: rate governor clamps untrusted Backoff / Retry-After ═══

def test_governor_clamps_huge_backoff_header():
    clock = FakeClock()
    gov = RateGovernor(sleep=clock.sleep, monotonic=clock.monotonic, max_backoff=300.0)
    t = SeqTransport([FakeResp(200, headers={"Backoff": "999999999"}), FakeResp(200)])
    gov.request(t, "GET", "http://host/a")     # captures a CLAMPED Backoff (not ~31 years)
    gov.request(t, "GET", "http://host/b")     # waits only the clamp
    assert clock.sleeps == [300.0]
    assert len(t.calls) == 2


def test_governor_clamps_huge_retry_after():
    clock = FakeClock()
    gov = RateGovernor(sleep=clock.sleep, monotonic=clock.monotonic, max_backoff=300.0)
    t = SeqTransport([FakeResp(429, headers={"Retry-After": "999999999"}), FakeResp(200)])
    resp = gov.request(t, "GET", "http://host/a")
    assert resp.status_code == 200
    assert clock.sleeps == [300.0]             # huge Retry-After clamped to the max, no multi-year hang


def test_governor_coerces_negative_retry_after_to_nonnegative():
    clock = FakeClock()
    gov = RateGovernor(sleep=clock.sleep, monotonic=clock.monotonic)
    t = SeqTransport([FakeResp(429, headers={"Retry-After": "-5"}), FakeResp(200)])
    resp = gov.request(t, "GET", "http://host/a")   # must never call time.sleep(-5) -> ValueError
    assert resp.status_code == 200
    assert clock.sleeps == [0.0]               # max(0, -5) -> 0


# ═══ Fix 2: negative-cache TTL prevents permanent 404 poisoning ═══

def test_cache_negative_ttl_rechecks_after_expiry(tmp_path):
    cache = JsonCache(tmp_path)
    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    # first contact 404s (e.g. a freshly-minted DOI mid-propagation) -> soft, timestamped miss
    a1 = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(404)), cache=cache, now=now, miss_ttl=100.0)
    assert a1.lookup_by_doi(DOI) is None
    # within TTL: served from the negative cache, raising transport never hit (offline re-run holds)
    a2 = DoiNegotiationAdapter(transport=RaisingTransport(), cache=cache, now=now, miss_ttl=100.0)
    assert a2.lookup_by_doi(DOI) is None
    # after TTL: the stale miss is re-checked, so the now-resolvable DOI is NOT permanently poisoned
    clock["t"] = 1000.0 + 101.0
    a3 = DoiNegotiationAdapter(transport=ConstTransport(FakeResp(200, body=CSL_VALID)),
                               cache=cache, now=now, miss_ttl=100.0)
    r = a3.lookup_by_doi(DOI)
    assert r is not None and r.title == _TITLE


# ═══ Fix 4: CrossrefAdapter.search never raises on a malformed record ═══

def test_crossref_search_never_raises_on_malformed_creators():
    a = CrossrefAdapter(transport=ConstTransport(FakeResp(200, body=CROSSREF_SEARCH)))
    for bad in ("Kohler", [7], ["K"], 42, {"x": 1}, None):
        out = a.search({"title": "x", "creators": bad})   # must NOT raise (the finding-4 crash)
        assert isinstance(out, list)
    # a non-dict record is also tolerated
    assert a.search("not a dict") == []
    assert a.search(None) == []
