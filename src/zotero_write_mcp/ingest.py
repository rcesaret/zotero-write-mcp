"""Deterministic routed PDF→metadata extractor for ingest (Phase-4, sprint S4; TC-8).

:func:`extract_pdf_metadata` composes the six-stage pipeline (PLAN2 §1.1): deterministic TRIAGE
(DOI/arXiv/ISBN regex over local text), deterministic ROUTE (born-digital vs scanned, language,
Path A GROBID vs Path B fixer-seed/LLM), STRUCTURED PARSE into a CANDIDATE field set, AUTHORITY
MATCH (REUSING :mod:`zotero_write_mcp.sources` — the S-V1 read-only clients), AGREEMENT SCORE
(REUSING :func:`zotero_write_mcp.validation.build_validation_result` — the S3 scorer + PINNED
gate), and a composed RETURN of ``{fields, per_field_source, agreement_confidence, needs_review}``.

Two invariants hold everywhere in this module (INV-COMP, FR-ING-3; PLAN2 §§5/6):
  1. ``agreement_confidence`` is the validation engine's calibrated ``p`` computed over CROSS-SOURCE
     AUTHORITY AGREEMENT — NEVER an LLM (or caller) self-report. Any confidence-like key on a
     candidate (``confidence``/``probability``/``certainty``/``score``/...) is defensively STRIPPED
     before scoring, and the candidate is additionally restricted to the pinned Zotero field set,
     so no code path lets a model-supplied number reach the gate.
  2. Path B (fixer-seed / LLM-header) is a CANDIDATE PRODUCER ONLY and NEVER auto-creates: any
     parse path other than a live GROBID header parse forces ``needs_review=True``
     ("path_b_never_auto_create", PLAN2 §5) regardless of how well authorities agree. The accept
     gate itself is :func:`validation.decide`'s, untouched — this module defines NO second floor
     and re-implements NO threshold logic.

Every side-effecting dependency (PDF text layer, GROBID HTTP, LLM header producer) is INJECTABLE
and degrades honestly when absent: on this host GROBID and AnyStyle are not installed, so parsing
degrades to the mineru-markdown-fixer seed and the result is always flagged for human review.
Pure logic otherwise; the unit suite is fully offline (fake transports/authorities/grobid/llm).
"""
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .dedup import first_author_surname, normalize_doi, normalize_title, normalize_year
from .sources import default_authorities, gather_by_doi, gather_by_search
from .validation import build_validation_result, load_calibration

# ── PINNED routing constants (PLAN2 §6; calibrate on the labeled routing set, do not hand-drift) ──
# Signal (a): embedded-text-layer character density. Page count is derived from the pdftotext-style
# form-feed convention (pages = text.count("\f") + 1); a text with no form feeds is treated as ONE
# page, so for page-count-unknown extractors the same constants act as TOTAL-chars thresholds
# (the documented fallback: >= 1000 total chars -> born_digital vote, <= 200 -> scanned vote).
BORN_DIGITAL_MIN_CHARS_PER_PAGE = 1000.0
SCANNED_MAX_CHARS_PER_PAGE = 200.0

# Signal (c): content_list.json block mix.
IMAGE_BLOCK_SCANNED_FRACTION = 0.5      # image-like fraction >= this -> scanned vote
TEXT_BLOCK_BORN_DIGITAL_FRACTION = 0.8  # text-like fraction >= this -> born_digital vote

# Language detection: fraction of alphabetic tokens found in a small English function-word set.
EN_STOPWORD_RATIO = 0.18

# Minimal mirror of MinerU's PUBLISHED content_list.json block-type constants. We deliberately do
# NOT import the harness fixer skill's mineru_common.py (cross-repo import of a core skill's
# internals is forbidden — core-skills-stability rule); these sets mirror the documented schema at
# opendatalab.github.io/MinerU/reference/output_files/ (flat reading-order list of typed blocks).
DISCARDED_TYPES = {"header", "footer", "page_number", "page_footnote", "discarded"}
IMAGE_LIKE_TYPES = {"image", "chart"}
TEXT_LIKE_TYPES = {"text", "title", "table", "equation", "list", "code"}

# INV-COMP defensive strip: keys that could smuggle a model/caller self-reported confidence into
# the candidate. Belt (this strip) AND suspenders (the CANDIDATE_FIELDS restriction below).
CONFIDENCE_LIKE_KEY_MARKERS = ("confidence", "probability", "certainty", "likelihood")
CONFIDENCE_LIKE_EXACT_KEYS = {"score", "p", "p_raw", "self_confidence"}

# The candidate field set (Zotero-native keys) a stage-3 parse may carry into scoring.
CANDIDATE_FIELDS = ("itemType", "title", "creators", "date", "DOI", "publicationTitle", "bookTitle")
# The fields stage 6 composes across authorities (bookTitle passes through from the candidate).
COMPOSED_FIELDS = ("title", "creators", "date", "DOI", "publicationTitle", "itemType")

GROBID_URL_ENV = "GROBID_URL"
GROBID_DEFAULT_URL = "http://localhost:8070"

# ── stage 1: TRIAGE — deterministic identifier regexes (local text only, no network) ─────────────

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/\S+)")
_ARXIV_NEW_RE = re.compile(r"\barxiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(r"\barxiv:\s*([a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})", re.IGNORECASE)
_ISBN_RE = re.compile(r"\bisbn(?:-1[03])?[:\s]\s*([0-9][0-9 -]{8,20}[0-9Xx])", re.IGNORECASE)

_TRAILING_PUNCT = ".,;:!?'\"”’"


def _clean_doi_match(raw: str) -> str:
    """Strip trailing punctuation and UNBALANCED closing brackets from a regex DOI hit —
    ``10.1234/abc).`` -> ``10.1234/abc`` while a legitimate ``10.1000/(sici)...`` keeps its parens."""
    d = str(raw).strip()
    while d:
        if d[-1] in _TRAILING_PUNCT:
            d = d[:-1]
            continue
        if d[-1] == ")" and d.count("(") < d.count(")"):
            d = d[:-1]
            continue
        if d[-1] == "]" and d.count("[") < d.count("]"):
            d = d[:-1]
            continue
        if d[-1] == "}" and d.count("{") < d.count("}"):
            d = d[:-1]
            continue
        break
    return d


def triage_identifiers(*texts: str) -> dict:
    """Regex-scan the given texts IN ORDER (deterministic: text layer first, then md) for the first
    DOI / arXiv id / ISBN. DOIs are normalized via the REUSED ``dedup.normalize_doi`` (handles
    doi.org URL forms and case). Returns ``{"doi", "arxiv", "isbn"}`` (each ``str | None``)."""
    doi: Optional[str] = None
    arxiv: Optional[str] = None
    isbn: Optional[str] = None
    for text in texts:
        if not text:
            continue
        text = str(text)
        if doi is None:
            m = _DOI_RE.search(text)
            if m:
                doi = normalize_doi({"DOI": _clean_doi_match(m.group(1))})
        if arxiv is None:
            m = _ARXIV_NEW_RE.search(text) or _ARXIV_OLD_RE.search(text)
            if m:
                arxiv = m.group(1).lower()
        if isbn is None:
            m = _ISBN_RE.search(text)
            if m:
                digits = re.sub(r"[ -]", "", m.group(1)).upper()
                if len(digits) in (10, 13):
                    isbn = digits
    return {"doi": doi, "arxiv": arxiv, "isbn": isbn}


# ── stage 2: ROUTE — three deterministic signals + a conservative combination rule (NO LLM) ──────

_EN_FUNCTION_WORDS = frozenset(
    "the of and to in is that it for as was with be by on not are this but have from or had they "
    "which you were her his an at will each all can there when who more no if out so said what up "
    "its about into than them then these some could".split()
)


def detect_language(text: str) -> str:
    """Deterministic English detector: fraction of alphabetic tokens in a small English
    function-word set >= :data:`EN_STOPWORD_RATIO` -> ``"en"``; any other non-empty text ->
    ``"other"``; no text -> ``"unknown"``. Pure function, no model, no network."""
    tokens = re.findall(r"[a-zA-Z]+", str(text or "").lower())
    if not tokens:
        return "unknown"
    ratio = sum(1 for t in tokens if t in _EN_FUNCTION_WORDS) / float(len(tokens))
    return "en" if ratio >= EN_STOPWORD_RATIO else "other"


def read_mineru_is_ocr(report_path: str, pdf_path: str = "", md_path: str = "") -> Optional[bool]:
    """Read ``settings.is_ocr`` from a MinerU ``--json`` run-report (a list of per-source dicts OR a
    single dict). Matches the entry whose ``output_dir``/``markdown``/``source`` relates to
    ``pdf_path``/``md_path`` (stem containment); if no entry matches but exactly one exists, uses
    it. Ambiguity, absence, or any read error -> ``None`` (the signal honestly abstains)."""
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    entries = data if isinstance(data, list) else [data]
    entries = [e for e in entries if isinstance(e, dict)]
    if not entries:
        return None

    stems = []
    for p in (pdf_path, md_path):
        if p:
            stem = Path(str(p)).stem.strip().lower()
            if stem:
                stems.append(stem)

    def _matches(entry: dict) -> bool:
        keys = " | ".join(
            str(entry.get(k, "")) for k in ("output_dir", "markdown", "source")
        ).replace("\\", "/").lower()
        return any(stem in keys for stem in stems)

    matched = [e for e in entries if _matches(e)] if stems else []
    if not matched and len(entries) == 1:
        matched = entries
    if len(matched) != 1:
        return None
    settings = matched[0].get("settings") or {}
    is_ocr = settings.get("is_ocr") if isinstance(settings, dict) else None
    return is_ocr if isinstance(is_ocr, bool) else None


def read_block_mix(content_list_path: str) -> Optional[tuple]:
    """Minimal reader over the DOCUMENTED MinerU ``content_list.json`` schema (flat reading-order
    list of dicts with ``type``): returns ``(image_like_fraction, text_like_fraction)`` over the
    non-discarded blocks. A ``middle.json``-shaped input (dict with ``pdf_info``) or anything else
    non-list is CLEANLY REJECTED -> ``None`` (documented: this reader abstains rather than guess).
    Missing/unreadable file or zero countable blocks -> ``None``."""
    try:
        with open(content_list_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    counted = [b for b in data
               if isinstance(b, dict) and b.get("type", "text") not in DISCARDED_TYPES]
    if not counted:
        return None
    n = float(len(counted))
    image = sum(1 for b in counted if b.get("type") in IMAGE_LIKE_TYPES)
    text = sum(1 for b in counted if b.get("type", "text") in TEXT_LIKE_TYPES)
    return image / n, text / n


def _grobid_available(grobid: Any) -> bool:
    try:
        return bool(grobid.available())
    except Exception:
        return False


def _route(*, text_layer: Optional[str], md_text: str, mineru_is_ocr: Optional[bool],
           block_mix: Optional[tuple], lang_hint: str, route_hint: str, grobid: Any) -> dict:
    """The PINNED deterministic routing table (parameter-registry; PLAN2 §6). Three signals vote
    born_digital/scanned (absent signals abstain); <2 votes OR any disagreement -> conservative
    ``scanned``. born-digital + English + not-humanities -> Path A (GROBID) when available, else
    Path B degraded; everything else -> Path B (the correct route, not degraded)."""
    reasons: list = []

    # signal (a): chars/page from the embedded text layer (pages via the form-feed convention).
    chars_per_page: Optional[float] = None
    if text_layer is not None:
        t = str(text_layer)
        chars_per_page = len(t) / float(t.count("\f") + 1)
    vote_a: Optional[str] = None
    if chars_per_page is not None:
        if chars_per_page >= BORN_DIGITAL_MIN_CHARS_PER_PAGE:
            vote_a = "born_digital"
        elif chars_per_page <= SCANNED_MAX_CHARS_PER_PAGE:
            vote_a = "scanned"

    # signal (b): MinerU --ocr auto verdict (settings.is_ocr from the JSON run-report).
    vote_b: Optional[str] = None
    if mineru_is_ocr is True:
        vote_b = "scanned"
    elif mineru_is_ocr is False:
        vote_b = "born_digital"

    # signal (c): content_list.json block mix.
    image_fraction: Optional[float] = None
    vote_c: Optional[str] = None
    if block_mix is not None:
        image_fraction, text_fraction = block_mix
        if image_fraction >= IMAGE_BLOCK_SCANNED_FRACTION:
            vote_c = "scanned"
        elif text_fraction >= TEXT_BLOCK_BORN_DIGITAL_FRACTION:
            vote_c = "born_digital"

    votes = {"chars_per_page": vote_a, "mineru_is_ocr": vote_b, "block_mix": vote_c}
    voting = [v for v in votes.values() if v is not None]
    if len(voting) < 2:
        source_kind = "scanned"
        reasons.append("conservative:fewer-than-2-voting-signals")
    elif len(set(voting)) > 1:
        source_kind = "scanned"
        reasons.append("conservative:voting-signals-disagree")
    else:
        source_kind = voting[0]
        reasons.append(f"signals-agree:{source_kind}")

    # language: an explicit hint wins over detection.
    if lang_hint:
        lang = str(lang_hint).strip().lower()
        reasons.append("lang:hint")
    else:
        lang = detect_language(md_text or (str(text_layer) if text_layer else ""))

    # route decision (PINNED table).
    if source_kind == "born_digital" and lang == "en" and route_hint != "humanities":
        if grobid is not None and _grobid_available(grobid):
            decision, degraded = "path_a", False
        else:
            decision, degraded = "path_b", True
            reasons.append("grobid_unavailable")
    else:
        decision, degraded = "path_b", False
        if route_hint == "humanities":
            reasons.append("route_hint:humanities")
        if source_kind == "scanned":
            reasons.append("source_kind:scanned")
        if lang != "en":
            reasons.append(f"lang:{lang}")

    return {
        "source_kind": source_kind,
        "lang": lang,
        "decision": decision,
        "parse_path": "none",          # stage 3 overwrites with grobid | llm_header | seed
        "degraded": degraded,
        "signals": {"chars_per_page": chars_per_page, "mineru_is_ocr": mineru_is_ocr,
                    "image_block_fraction": image_fraction, "votes": votes},
        "reasons": reasons,
    }


# ── stage 3: STRUCTURED PARSE — candidate producers (all candidates, never confidences) ──────────

def strip_confidence_keys(candidate: dict) -> dict:
    """INV-COMP defensive strip: remove any confidence-like key a caller/LLM might smuggle onto a
    candidate (``confidence``/``probability``/``certainty``/``likelihood`` substrings, plus the
    exact keys ``score``/``p``/``p_raw``/``self_confidence``). The scorer never reads these anyway
    (it reads only bibliographic fields), but nothing confidence-shaped may even *travel* toward
    the gate."""
    out = {}
    for k, v in (candidate or {}).items():
        kl = str(k).lower()
        if kl in CONFIDENCE_LIKE_EXACT_KEYS:
            continue
        if any(marker in kl for marker in CONFIDENCE_LIKE_KEY_MARKERS):
            continue
        out[k] = v
    return out


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


_BIBTEX_FENCE_RE = re.compile(r"```\s*(?:bibtex|bib)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_BIBTEX_ENTRY_RE = re.compile(r"(@[A-Za-z]+\s*\{.*?\n\})", re.DOTALL)
_YAML_FM_RE = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_YAML_SEED_KEYS = ("title", "author", "authors", "year", "date", "doi")


def _parse_yaml_frontmatter(md_text: str) -> dict:
    """Minimal line-based YAML front-matter reader (title/author/year/date/doi scalars only — no
    pyyaml dependency; the fixer's front-matter is flat for these keys)."""
    m = _YAML_FM_RE.match(md_text or "")
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip().strip("'\"")
        if key in _YAML_SEED_KEYS and val:
            out["author" if key == "authors" else key] = val
    return out


def _creators_from_string(s: str) -> list:
    creators = []
    for name in re.split(r"\s+and\s+|;", str(s)):
        name = name.strip().strip("'\"")
        if not name:
            continue
        if "," in name:
            last, _, first = name.partition(",")
            creators.append({"creatorType": "author", "lastName": last.strip(),
                             "firstName": first.strip()})
        else:
            first, _, last = name.rpartition(" ")
            if first and last:
                creators.append({"creatorType": "author", "firstName": first.strip(),
                                 "lastName": last.strip()})
            else:
                creators.append({"creatorType": "author", "lastName": name, "firstName": ""})
    return creators


def _seed_candidate(md_text: str) -> Optional[dict]:
    """Path-B candidate from the mineru-markdown-fixer corrected md: the BibTeX block (parsed via
    the REUSED ``utils.parse_bibtex``) primary, the YAML front-matter filling gaps. AnyStyle is
    absent on this host, so references parsing is skipped (noted honestly in the route evidence)."""
    if not md_text:
        return None
    cand: dict = {}
    m = _BIBTEX_FENCE_RE.search(md_text) or _BIBTEX_ENTRY_RE.search(md_text)
    if m:
        from .utils import parse_bibtex  # lazy: utils imports httpx/bibtexparser at module top
        parsed = parse_bibtex(m.group(1))
        if parsed:
            for k in CANDIDATE_FIELDS:
                v = parsed.get(k)
                if v:
                    cand[k] = v
    fm = _parse_yaml_frontmatter(md_text)
    for src_key, zot_key in (("title", "title"), ("doi", "DOI"), ("year", "date"),
                             ("date", "date")):
        v = fm.get(src_key)
        if v and not cand.get(zot_key):
            cand[zot_key] = v
    if fm.get("author") and not cand.get("creators"):
        cand["creators"] = _creators_from_string(fm["author"])
    return cand or None


# ── GROBID client (Path A; injectable transport, mirrors the sources.py adapter posture) ─────────

_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def _parse_tei_header(tei_xml: str) -> Optional[dict]:
    """TEI header -> candidate dict (Zotero-native keys) via stdlib ``xml.etree``. Returns ``None``
    on any parse failure or when no title is present. Never raises."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(tei_xml)
    except Exception:
        return None

    def _txt(el: Any) -> str:
        return "".join(el.itertext()).strip() if el is not None else ""

    title = _txt(root.find(".//tei:fileDesc/tei:titleStmt/tei:title", _TEI_NS))
    if not title:
        return None
    creators = []
    for pers in root.findall(".//tei:sourceDesc//tei:author/tei:persName", _TEI_NS):
        forenames = [_txt(e) for e in pers.findall("tei:forename", _TEI_NS)]
        surname = _txt(pers.find("tei:surname", _TEI_NS))
        if surname or any(forenames):
            creators.append({"creatorType": "author",
                             "firstName": " ".join(f for f in forenames if f),
                             "lastName": surname})
    date = ""
    for d in root.findall(".//tei:date", _TEI_NS):
        when = d.get("when") or ""
        if when:
            date = when
            break
    doi = ""
    for idno in root.findall(".//tei:idno", _TEI_NS):
        if (idno.get("type") or "").upper() == "DOI":
            doi = _txt(idno)
            break
    venue = _txt(root.find(".//tei:monogr/tei:title", _TEI_NS))

    cand = {"itemType": "journalArticle", "title": title, "creators": creators, "date": date}
    if doi:
        cand["DOI"] = doi
    if venue:
        cand["publicationTitle"] = venue
    return cand


class GrobidClient:
    """Thin GROBID header-parse client. Base URL from ``GROBID_URL`` (default
    ``http://localhost:8070``). ``transport`` is injectable — any object with a
    ``.request(method, url, **kw)`` method or a bare callable — so unit tests run fully offline;
    ``None`` uses httpx lazily. NEVER raises (mirrors the sources.py adapter posture: any error
    degrades to ``False``/``None``)."""

    def __init__(self, base_url: Optional[str] = None, transport: Any = None):
        self.base_url = str(base_url or os.environ.get(GROBID_URL_ENV)
                            or GROBID_DEFAULT_URL).rstrip("/")
        self._transport = transport

    def _request(self, method: str, url: str, **kw: Any) -> Any:
        try:
            t = self._transport
            if t is None:
                import httpx
                return httpx.request(method, url, timeout=30.0, **kw)
            req = getattr(t, "request", None)
            if callable(req):
                return req(method, url, **kw)
            return t(method, url, **kw)
        except Exception:
            return None

    def available(self) -> bool:
        resp = self._request("GET", f"{self.base_url}/api/isalive")
        if resp is None or getattr(resp, "status_code", 0) != 200:
            return False
        body = str(getattr(resp, "text", "") or "").strip().lower()
        return body not in ("", "false", "0")

    def process_header(self, pdf_path: str) -> Optional[dict]:
        try:
            with open(pdf_path, "rb") as f:
                data = f.read()
        except Exception:
            data = b""
        name = os.path.basename(str(pdf_path)) or "input.pdf"
        resp = self._request("POST", f"{self.base_url}/api/processHeaderDocument",
                             files={"input": (name, data, "application/pdf")})
        if resp is None or getattr(resp, "status_code", 0) != 200:
            return None
        try:
            return _parse_tei_header(str(getattr(resp, "text", "") or ""))
        except Exception:
            return None


# ── stage 6: fields composition (consensus > single-authority match > candidate) ─────────────────

# Best-effort authority item-type -> Zotero itemType map (Crossref/CSL/OpenAlex spellings).
_ITEM_TYPE_TO_ZOTERO = {
    "journal-article": "journalArticle", "article-journal": "journalArticle",
    "journalarticle": "journalArticle", "article": "journalArticle",
    "book": "book", "monograph": "book",
    "book-chapter": "bookSection", "chapter": "bookSection", "booksection": "bookSection",
    "proceedings-article": "conferencePaper", "paper-conference": "conferencePaper",
    "conferencepaper": "conferencePaper",
    "thesis": "thesis", "dissertation": "thesis",
    "report": "report", "dataset": "dataset",
}


def _zotero_item_type(value: Any) -> str:
    v = str(value or "").strip()
    return _ITEM_TYPE_TO_ZOTERO.get(v.lower().replace(" ", ""), v)


def _rec_as_dict(r: Any) -> dict:
    if r is None:
        return {}
    as_dict = getattr(r, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return r if isinstance(r, dict) else {}


def _authority_creators_to_zotero(creators: Any) -> list:
    out = []
    for c in creators or []:
        if not isinstance(c, dict):
            out.append({"creatorType": "author", "lastName": str(c), "firstName": ""})
            continue
        if c.get("lastName") or c.get("firstName"):
            out.append({"creatorType": "author",
                        "firstName": str(c.get("firstName") or ""),
                        "lastName": str(c.get("lastName") or "")})
        elif c.get("name"):
            out.append({"creatorType": "author", "name": str(c["name"])})
    return out


def _authority_zotero_view(rec: dict) -> dict:
    """Map a NormalizedRecord dict onto Zotero-native keys (container_title -> publicationTitle,
    year -> date, item_type -> best-effort Zotero itemType). Verbatim values, no fabrication."""
    return {
        "title": rec.get("title") or "",
        "creators": _authority_creators_to_zotero(rec.get("creators")),
        "date": rec.get("year") or "",
        "DOI": rec.get("doi") or "",
        "publicationTitle": rec.get("container_title") or "",
        "itemType": _zotero_item_type(rec.get("item_type")),
    }


def _norm_value(field: str, value: Any) -> str:
    """Per-field comparison key, REUSING the dedup/validation normalizers (no drift)."""
    if field in ("title", "publicationTitle"):
        return normalize_title(value)
    if field == "creators":
        return first_author_surname({"creators": value or []})
    if field == "date":
        return normalize_year({"date": value})
    if field == "DOI":
        return normalize_doi({"DOI": value}) or ""
    if field == "itemType":
        return _zotero_item_type(value).lower()
    return str(value or "")


def _compose_fields(candidate: dict, cand_origins: dict, authority_dicts: list) -> tuple:
    """Deterministic composition: >=2 distinct authorities agreeing on the normalized value ->
    the VERBATIM value from the first authority (in list order) carrying it, source
    ``consensus:<names>``; else candidate matching >=1 authority -> that authority's verbatim
    value, source ``<authority name>``; else the candidate value, source = its origin."""
    fields: dict = {}
    per_field_source: dict = {}
    views = [((d.get("source") or "authority"), _authority_zotero_view(d))
             for d in authority_dicts]

    for f in COMPOSED_FIELDS:
        entries = []                                   # (authority name, verbatim, norm key)
        for name, view in views:
            v = view.get(f)
            if not v:
                continue
            nk = _norm_value(f, v)
            if nk:
                entries.append((name, v, nk))
        by_nk: dict = {}
        for name, v, nk in entries:
            by_nk.setdefault(nk, []).append((name, v))

        consensus_key = None
        for name, v, nk in entries:                    # authority list order is deterministic
            if len({n for n, _ in by_nk[nk]}) >= 2:
                consensus_key = nk
                break

        cand_v = candidate.get(f)
        cand_nk = _norm_value(f, cand_v) if cand_v else ""

        if consensus_key is not None:
            group = by_nk[consensus_key]
            names: list = []
            for n, _ in group:
                if n not in names:
                    names.append(n)
            fields[f] = group[0][1]
            per_field_source[f] = "consensus:" + ",".join(names)
        elif cand_v and cand_nk and any(nk == cand_nk for _, _, nk in entries):
            name, v, _nk = next(e for e in entries if e[2] == cand_nk)
            fields[f] = v
            per_field_source[f] = name
        elif cand_v:
            fields[f] = cand_v
            per_field_source[f] = cand_origins.get(f, "fixer_seed")

    # Candidate-only passthrough fields (e.g. bookTitle) keep their origin label.
    for f in CANDIDATE_FIELDS:
        if f not in fields and f not in COMPOSED_FIELDS and candidate.get(f):
            fields[f] = candidate[f]
            per_field_source[f] = cand_origins.get(f, "fixer_seed")

    return fields, per_field_source


# ── the six-stage pipeline (TC-8) ─────────────────────────────────────────────────────────────────

def extract_pdf_metadata(
    pdf_path: str,
    md_path: str = "",
    content_list_path: str = "",
    mineru_report_path: str = "",
    lang_hint: str = "",
    route_hint: str = "",
    *,
    text_extractor: Optional[Callable[[str], Optional[str]]] = None,
    grobid: Any = None,
    llm: Optional[Callable[[str], Optional[dict]]] = None,
    authorities: Optional[list] = None,
    calibration_path: Any = None,
) -> dict:
    """TC-8: PDF -> ``{fields, per_field_source, agreement_confidence, needs_review, ...}`` via the
    deterministic six-stage pipeline. READ-ONLY: local file reads + external-authority reads only;
    zero Zotero writes. ``agreement_confidence`` is ALWAYS ``build_validation_result``'s calibrated
    ``p`` over cross-source authority agreement (INV-COMP); ``needs_review`` derives from the PINNED
    gate's ``decision`` plus the Path-B never-auto-create rule — no threshold logic lives here.

    Injectables (production callers pass ``grobid=GrobidClient()`` and leave the rest ``None``):
      text_extractor: embedded-text-layer reader (routing signal a); ``None`` -> signal absent.
      grobid: object with ``.available()`` and ``.process_header(pdf_path)`` (Path A).
      llm: Path-B candidate header producer — a CANDIDATE ONLY, confidence-stripped, never scored
           as confidence (tests only; production ``None``).
      authorities: list of :class:`sources.Authority`; ``None`` -> ``default_authorities()``.
                   An explicit empty list stays empty (never silently falls back to live adapters).
      calibration_path: passthrough to :func:`validation.load_calibration`.
    """
    # local inputs (each may honestly be absent)
    text_layer: Optional[str] = None
    if text_extractor is not None:
        try:
            text_layer = text_extractor(pdf_path)
        except Exception:
            text_layer = None
    md_text = _read_text(md_path) if md_path else ""

    # 1. TRIAGE (deterministic order: text layer, then md — which carries the fixer seed).
    identifiers = triage_identifiers(text_layer or "", md_text)

    # 2. ROUTE (deterministic table, no LLM).
    route = _route(
        text_layer=text_layer,
        md_text=md_text,
        mineru_is_ocr=(read_mineru_is_ocr(mineru_report_path, pdf_path, md_path)
                       if mineru_report_path else None),
        block_mix=read_block_mix(content_list_path) if content_list_path else None,
        lang_hint=lang_hint,
        route_hint=route_hint,
        grobid=grobid,
    )

    # 3. STRUCTURED PARSE -> candidate field set (a candidate is only ever a candidate).
    candidate: Optional[dict] = None
    origin = "none"
    if route["decision"] == "path_a":
        header = None
        try:
            header = grobid.process_header(pdf_path)
        except Exception:
            header = None
        if header:
            candidate, origin = dict(header), "grobid"
            route["parse_path"] = "grobid"
        else:
            route["degraded"] = True
            route["reasons"].append("grobid_no_header")
    if candidate is None:
        if llm is not None:
            out = None
            try:
                out = llm(md_text)
            except Exception:
                out = None
            if isinstance(out, dict) and out:
                candidate, origin = dict(out), "llm_header"
                route["parse_path"] = "llm_header"
        if candidate is None:
            seed = _seed_candidate(md_text)
            if seed:
                candidate, origin = seed, "fixer_seed"
                route["parse_path"] = "seed"

    # INV-COMP: strip confidence-like keys from EVERY candidate origin, then restrict to the
    # pinned Zotero field set — nothing confidence-shaped can travel toward the gate.
    if candidate is not None:
        candidate = strip_confidence_keys(candidate)
        candidate = {k: v for k, v in candidate.items() if k in CANDIDATE_FIELDS and v}
        if not candidate:
            candidate = None

    cand_origins = {k: origin for k in (candidate or {})}
    if candidate is not None and not candidate.get("DOI") and identifiers["doi"]:
        candidate["DOI"] = identifiers["doi"]
        cand_origins["DOI"] = "triage"

    if candidate is None:
        route["parse_path"] = "none"
        return {
            "fields": {},
            "per_field_source": {},
            "agreement_confidence": 0.0,
            "needs_review": True,
            "needs_review_reasons": ["no_candidate"],
            "decision": "flag",
            "conflicts": [],
            "evidence": ["no candidate field set produced "
                         "(no grobid header, no llm header, no fixer seed)"],
            "route": route,
            "validation": {"p": 0.0, "p_raw": 0.0, "consensus": False, "consensus_count": 0,
                           "id_agreement": False, "calibration_version": None,
                           "available_authorities": [], "answered_authorities": []},
            "identifiers": identifiers,
        }

    # 4. AUTHORITY MATCH (REUSE sources.py; an explicit [] stays [] — no silent live fallback).
    if authorities is None:
        authorities = default_authorities()
    doi = identifiers["doi"] or normalize_doi(candidate)
    if doi:
        gathered = gather_by_doi(doi, authorities)
    else:
        gathered = gather_by_search(candidate, authorities)

    # 5. AGREEMENT SCORE (REUSE validation.py — scorer, calibration, and the PINNED gate).
    verdict = build_validation_result(
        candidate, gathered.records, load_calibration(calibration_path),
        doi_lookup_attempted=bool(doi), extra_evidence=gathered.evidence,
    )

    # needs_review derives from decide()'s output + the Path-B never-auto-create rule (PLAN2 §5).
    needs_review = (verdict["decision"] != "accept") or (route["parse_path"] != "grobid")
    reasons: list = []
    if verdict["decision"] != "accept":
        reasons.append(f"decision:{verdict['decision']}")
    if any((c.get("kind") if isinstance(c, dict) else None) == "id_disagreement"
           for c in verdict["conflicts"]):
        reasons.append("id_disagreement")
    if route["parse_path"] != "grobid":
        reasons.append("path_b_never_auto_create")
    for r in route["reasons"]:
        if r in ("grobid_unavailable", "grobid_no_header") and r not in reasons:
            reasons.append(r)

    # 6. RETURN — composed fields + the full computational verdict.
    fields, per_field_source = _compose_fields(
        candidate, cand_origins, [_rec_as_dict(r) for r in gathered.records])
    return {
        "fields": fields,
        "per_field_source": per_field_source,
        "agreement_confidence": verdict["p"],   # INV-COMP: ALWAYS the calibrated agreement p
        "needs_review": needs_review,
        "needs_review_reasons": reasons,
        "decision": verdict["decision"],
        "conflicts": verdict["conflicts"],
        "evidence": verdict["evidence"],
        "route": route,
        "validation": {
            "p": verdict["p"],
            "p_raw": verdict["p_raw"],
            "consensus": verdict["consensus"],
            "consensus_count": verdict["consensus_count"],
            "id_agreement": verdict["id_agreement"],
            "calibration_version": verdict["calibration_version"],
            "available_authorities": gathered.available,
            "answered_authorities": gathered.answered,
        },
        "identifiers": identifiers,
    }
