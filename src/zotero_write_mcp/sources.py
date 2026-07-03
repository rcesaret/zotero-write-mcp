"""Read-only bibliographic-authority source clients (Phase-3 validation, sprint S-V1).

This module talks ONLY to external bibliographic authorities (doi.org content-negotiation,
Crossref, OpenAlex, Semantic Scholar, DataCite, ORCID). It is the single input contract the
Phase-3 validation scorer/gate (a LATER sprint, S3) and the Phase-4 ingest join (S4) bind to.

Two hard boundaries define this module:

  1. READ-ONLY. It makes ZERO writes to Zotero by any path — no gateway, no read-server, no local
     API, no SQLite, no pyzotero. Every call here is a GET against an external authority. There is
     no scorer, no accept/flag/reject gate, no calibration, no MCP tool, no hook, no PROV record —
     all of that is S3, deliberately out of scope here.

  2. NEVER threshold Crossref's raw ``score``. Crossref's ``message.items[].score`` is a Solr/Lucene
     TF-IDF relevance number — NOT a probability and NOT comparable across queries. It is used ONLY
     to *order* candidate records within a single query (``CrossrefAdapter.search``); it must never
     cross a numeric threshold anywhere in this module. (FR-VAL-4; parameter-registry "Crossref raw
     score" PINNED anti-pattern.)

Design (reuse-first, mirrors the hardened engine spine — no reinvention, no drift):
  * normalization  -> reuse ``dedup`` normalizers (normalize_doi / normalize_year) so downstream
                      comparison in S3 is drift-free (the scorer normalizes titles with the same
                      ``dedup.normalize_title`` on the verbatim titles emitted here).
  * rate governor  -> ``RateGovernor`` mirrors ``gateway.WriteGateway._request`` (monotonic-clock
                      Backoff wait + 429/Retry-After retry, injectable ``sleep``/``monotonic``).
  * HTTP transport -> ``HttpxReadTransport`` mirrors ``gateway.HttpxTransport`` (duck-typed, returns
                      the raw response, never calls ``raise_for_status``) so every adapter is
                      unit-testable offline with a fake transport.
  * cache          -> ``JsonCache`` mirrors ``provenance.ProvenanceStore`` blob store (content-
                      addressed, sharded ``<sha256[:2]>/<sha256>``, atomic ``os.replace`` write)
                      under ``runtime/validation-cache/``: idempotent + offline re-runnable, keeps
                      the OpenAlex calibration loop to one network hit per record (~$1/day ceiling).

NOTE on Crossref reuse: ``utils.resolve_doi`` (utils.py:15) is intentionally NOT called here — it
does a hardcoded, non-injectable ``httpx.get`` with the placeholder mailto ``research@example.com``
(utils.py:21), which would defeat offline testing, rate-governing, caching, and the env-mailto fix.
We inline the Crossref base URL (NOT ``from .utils import`` — utils.py eagerly imports httpx +
bibtexparser) and mirror ``resolve_doi``'s Crossref->field mapping through the injectable transport
instead (per S-V1 §D: "wrap in governed+cached client; fix the mailto from env").
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .dedup import normalize_doi, normalize_year
from .provenance import canonical_json, sha256_hex

# Crossref REST base. We deliberately do NOT ``from .utils import CROSSREF_URL`` to reuse this constant:
# utils.py imports httpx + bibtexparser at module top level, so importing it would eagerly pull those
# heavy deps merely by importing THIS module — defeating the lazy-httpx / offline-clean-import invariant
# this module guarantees. The base URL is stable, so inlining it avoids the coupling with no drift risk.
CROSSREF_BASE = "https://api.crossref.org"

# Upper bound (seconds) on any wait derived from an external authority's Backoff / Retry-After header.
# A DELIBERATE divergence from an exact ``gateway._request`` mirror: the write gateway talks to ONE
# trusted host, whereas this governor faces SIX external authorities, so a hostile/garbage header must
# not be able to pin a host (or the whole single-threaded batch) for an unbounded time. Normal values
# (seconds) are unaffected; this only clamps the pathological case and coerces a negative wait to >= 0.
MAX_BACKOFF = 300.0

# A neutral, non-placeholder polite-pool mailto. NOT the utils.py:21 example.com placeholder and NOT
# a personal address; a real per-record mailto is set from the CROSSREF_MAILTO env var when present.
DEFAULT_MAILTO = "zotero-write-mcp@users.noreply.github.com"
USER_AGENT = f"zotero-write-mcp (mailto:{DEFAULT_MAILTO})"

# CSL-JSON content-negotiation Accept. NOTE (verified live 2026-07-02): Crossref's current transform
# endpoint REJECTS the canonical ``application/vnd.citationstyle.csl+json`` with HTTP 406 and honors
# only the older ``application/citeproc+json`` (an identical CSL-JSON payload); DataCite honors the
# canonical type. Sending BOTH (canonical first, citeproc as q=0.9 fallback) keeps DOI content-
# negotiation registrar-agnostic so it resolves Crossref AND DataCite DOIs.
CSL_ACCEPT = "application/vnd.citationstyle.csl+json, application/citeproc+json;q=0.9"


# ── small helpers ───────────────────────────────────────────────────────────────

def _as_list(x: Any) -> list:
    return x if isinstance(x, list) else []


def _as_dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _first_str(x: Any) -> str:
    """First string of a str|list value (Crossref/CSL ``title`` and ``container-title`` are lists)."""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        for v in x:
            if isinstance(v, str) and v:
                return v
    return ""


def _norm_year(value: Any) -> str:
    """Route any year-ish value through the reused ``dedup.normalize_year`` (drift-free)."""
    if value in (None, ""):
        return ""
    return normalize_year({"date": str(value)})


def _year_from_dateparts(obj: Any) -> str:
    """Extract the year from a CSL/Crossref date object: ``{"date-parts": [[YYYY, ...]]}``."""
    dp = _as_dict(obj).get("date-parts")
    if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
        return _norm_year(dp[0][0])
    return ""


def _name_dict(author: Any) -> dict:
    """Map an author entry to a creator dict. Structured given/family -> {firstName,lastName};
    a name-only entry -> {"name": ...}. Both shapes are consumed by the reused dedup surname
    extractors (which read ``lastName`` OR ``name``). Never fabricates."""
    a = _as_dict(author)
    family = a.get("family") or a.get("familyName") or a.get("lastName")
    given = a.get("given") or a.get("givenName") or a.get("firstName")
    if family or given:
        out = {"firstName": str(given or ""), "lastName": str(family or "")}
    else:
        nm = a.get("name") or a.get("literal") or a.get("display_name")
        if not nm:
            return {}
        out = {"name": str(nm)}
    orcid = _orcid_id(a.get("orcid") or a.get("ORCID"))
    if orcid:
        out["orcid"] = orcid
    return out


def _orcid_id(value: Any) -> str:
    """Reduce an ORCID URL or bare iD to the canonical ``0000-0000-0000-000X`` form ('' if none)."""
    if not value:
        return ""
    s = str(value).strip().rstrip("/")
    return s.rsplit("/", 1)[-1].upper() if "/" in s else s.upper()


def _score_of(item: Any) -> float:
    """Crossref candidate relevance ``score`` — used ONLY as a sort key in ``CrossrefAdapter.search``.
    It is a Solr/Lucene TF-IDF value, NEVER a probability and NEVER compared to a threshold here."""
    try:
        return float(_as_dict(item).get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _header_int(headers: Any, name: str) -> Optional[int]:
    """Case-insensitive integer header lookup (mirrors the gateway governor's header parsing)."""
    if not headers:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return None
    for k, v in items:
        if str(k).lower() == name.lower():
            try:
                return int(str(v).strip())
            except (TypeError, ValueError):
                return None
    return None


# ── the canonical normalized output shape ───────────────────────────────────────

@dataclass
class NormalizedRecord:
    """The ONE canonical shape every adapter emits, so the S3 scorer consumes a single schema
    regardless of source. Typed fields are normalized (doi/year via the reused dedup normalizers;
    titles kept verbatim for display — S3 compares them with the shared ``dedup.normalize_title``,
    which is what keeps comparison drift-free). ``raw`` holds the untouched source payload for audit.
    Nothing here is fabricated: every value comes verbatim from the named authority's response."""

    source: str
    title: str = ""
    creators: list = field(default_factory=list)   # [{firstName,lastName} | {name} | +orcid]
    year: str = ""
    doi: Optional[str] = None
    container_title: str = ""                       # venue
    item_type: str = ""
    external_ids: dict = field(default_factory=dict)  # {doi, arxiv, pmid, openalex, orcid, ...}
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "creators": self.creators,
            "year": self.year,
            "doi": self.doi,
            "container_title": self.container_title,
            "item_type": self.item_type,
            "external_ids": self.external_ids,
            "raw": self.raw,
        }


def _mk(rec: NormalizedRecord) -> Optional[NormalizedRecord]:
    """Return the record only if it carries usable bibliographic identity; a 200 response whose
    payload is empty/malformed (no title, doi, ids, or creators) -> None, so an authority that
    answered with nothing does not count as a record."""
    return rec if (rec.title or rec.doi or rec.external_ids or rec.creators) else None


@runtime_checkable
class Authority(Protocol):
    """The duck-typed protocol every adapter satisfies. S3 and S4 bind to this contract."""

    name: str

    def available(self) -> bool: ...
    def lookup_by_doi(self, doi: str) -> Optional[NormalizedRecord]: ...
    def search(self, record: dict) -> list: ...  # list[NormalizedRecord]


# ── injectable HTTP transport (mirrors gateway.HttpxTransport) ───────────────────

class HttpxReadTransport:
    """Live read transport. Mirrors ``gateway.HttpxTransport``'s contract: returns the raw response
    (``.status_code`` / ``.json()`` / ``.headers``) and — crucially — never calls ``raise_for_status``,
    so the governor/adapter inspect the status themselves. ``httpx`` is imported lazily so the module
    imports cleanly and offline unit tests (which inject a fake transport) never touch the network."""

    def __init__(self, client: Any = None, *, timeout: float = 15.0):
        self._client = client
        self._timeout = timeout

    def request(self, method: str, url: str, *, headers: Optional[dict] = None,
                params: Optional[dict] = None):
        import httpx
        if self._client is not None:
            return self._client.request(method, url, headers=headers, params=params)
        return httpx.request(method, url, headers=headers, params=params,
                             timeout=self._timeout, follow_redirects=True)


# ── per-host rate governor (mirrors gateway.WriteGateway._request) ───────────────

class RateGovernor:
    """Per-host backpressure, ported from ``gateway.WriteGateway._request`` (gateway.py:180-196):
    wait out any pending ``Backoff``, capture a new one from the response, retry on 429 honoring
    ``Retry-After``. ``sleep``/``monotonic`` are injectable so tests advance a fake clock with zero
    real waiting. Adds an optional per-host ``min_interval`` spacing (external authorities publish
    per-second limits the write gateway did not need); defaults to 0 so behaviour matches the
    gateway when spacing is not requested."""

    def __init__(self, *, min_interval: float = 0.0,
                 sleep: Optional[Callable[[float], None]] = None,
                 monotonic: Optional[Callable[[], float]] = None,
                 max_429_retries: int = 3, max_backoff: float = MAX_BACKOFF):
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._max_429_retries = max_429_retries
        self._min_interval = float(min_interval)
        self._max_backoff = float(max_backoff)
        self._resume_at = 0.0   # monotonic time before which no new request may be sent (from Backoff)
        self._last_call = -1e18

    def request(self, transport: Any, method: str, url: str, **kw):
        now = self._monotonic()
        wait = 0.0
        if now < self._resume_at:
            wait = self._resume_at - now
        if self._min_interval:
            wait = max(wait, (self._last_call + self._min_interval) - now)
        if wait > 0:
            self._sleep(min(wait, self._max_backoff))
        attempts = 0
        while True:
            resp = transport.request(method, url, **kw)
            self._last_call = self._monotonic()
            backoff = _header_int(getattr(resp, "headers", None), "Backoff")
            if backoff:
                # clamp an untrusted Backoff: a hostile/garbage header must not pin this host for eons
                self._resume_at = self._monotonic() + min(float(backoff), self._max_backoff)
            if getattr(resp, "status_code", None) == 429 and attempts < self._max_429_retries:
                attempts += 1
                retry_after = float(_header_int(resp.headers, "Retry-After") or 1)
                self._sleep(min(max(0.0, retry_after), self._max_backoff))
                continue
            return resp


# ── content-addressed JSON cache (mirrors provenance.ProvenanceStore blobs) ──────

def default_cache_root() -> Path:
    """Cache root: ``$ZOT_VALIDATION_CACHE_DIR`` or ``runtime/validation-cache`` relative to CWD.
    No absolute path is hardcoded (harness config rule); tests inject an explicit tmp dir."""
    env = os.environ.get("ZOT_VALIDATION_CACHE_DIR")
    return Path(env) if env else Path("runtime") / "validation-cache"


class JsonCache:
    """A content-addressed query->response JSON cache, mirroring ``provenance.ProvenanceStore``'s
    blob store: keyed by ``sha256(source | doi | normalized-query)``, sharded ``<sha256[:2]>/<sha256>``,
    written atomically (``tmp -> fsync -> os.replace``) and never mutated in place. Idempotent and
    offline re-runnable — a repeated lookup is one on-disk read with zero network."""

    def __init__(self, root: "os.PathLike[str] | str"):
        self.root = Path(root)

    def key(self, *parts: Any) -> str:
        raw = " ".join("" if p is None else str(p) for p in parts).encode("utf-8")
        return sha256_hex(raw)

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()

    def get(self, digest: str) -> Any:
        p = self._path(digest)
        if not p.exists():
            return None
        try:
            import json
            return json.loads(p.read_bytes().decode("utf-8"))
        except Exception:
            return None

    def put(self, digest: str, obj: Any, *, overwrite: bool = False) -> str:
        """Store ``obj`` as canonical JSON at the sharded path, written atomically (``os.replace``).
        Idempotent by default (an existing blob is kept, never rewritten); ``overwrite=True`` atomically
        replaces it — used only to refresh an expired negative-cache miss. A resolved record is never
        re-fetched (a cached SUCCESS short-circuits before any network call), so a cached success is
        never overwritten in practice."""
        dest = self._path(digest)
        if dest.exists() and not overwrite:
            return digest
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_json(obj)
        tmp = dest.parent / f"{digest}.tmp-{uuid.uuid4().hex}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)  # atomic on the same filesystem (replaces an existing dest atomically)
        return digest


# ── base adapter (transport + governor + cache; NEVER raises) ────────────────────

class _BaseAdapter:
    """Shared machinery for every authority adapter. A GET is cache-checked first (offline
    re-runnable), then governed, then status-handled. Any error / non-200 (bar a cacheable 404)
    degrades to ``None`` — an adapter NEVER raises and NEVER aborts a batch."""

    name = "base"
    min_interval = 0.0

    def __init__(self, transport: Any = None, *, cache: Optional[JsonCache] = None,
                 governor: Optional[RateGovernor] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 monotonic: Optional[Callable[[], float]] = None,
                 now: Optional[Callable[[], float]] = None,
                 miss_ttl: float = 86400.0):
        self._t = transport if transport is not None else HttpxReadTransport()
        self._cache = cache
        self._gov = governor or RateGovernor(min_interval=self.min_interval, sleep=sleep,
                                             monotonic=monotonic)
        self._now = now or time.time         # wall-clock, for the bounded negative-cache TTL
        self._miss_ttl = float(miss_ttl)

    def available(self) -> bool:
        return True

    def _unavailable_note(self) -> str:
        return f"{self.name}: unavailable"

    def _get_json(self, url: str, *, params: Optional[dict] = None,
                  headers: Optional[dict] = None,
                  cache_parts: Optional[list] = None) -> Optional[dict]:
        ck = None
        if self._cache is not None and cache_parts is not None:
            ck = self._cache.key(self.name, *cache_parts)
            cached = self._cache.get(ck)
            if cached is not None:
                m = _as_dict(cached)
                if m.get("__miss__"):
                    ts = m.get("ts")
                    if isinstance(ts, (int, float)) and (self._now() - ts) < self._miss_ttl:
                        return None                  # fresh negative cache -> served, no network
                    # stale miss -> fall through and re-fetch (a since-resolved DOI is not poisoned)
                else:
                    return cached                    # cached successful response (stable, permanent)
        try:
            resp = self._gov.request(self._t, "GET", url, params=params, headers=headers)
        except Exception:
            return None                              # transient / network error -> degrade, no cache
        status = getattr(resp, "status_code", None)
        if status == 200:
            try:
                body = resp.json()
            except Exception:
                return None
            if ck is not None:
                try:
                    self._cache.put(ck, body, overwrite=True)   # replace a stale miss with the record
                except Exception:
                    pass
            return body
        if status == 404 and ck is not None:
            try:
                # a soft, TIME-STAMPED negative cache: keeps within-run / same-day re-runs offline, but
                # expires so a transient 404 (e.g. a freshly-minted DOI mid-propagation) is not permanent
                self._cache.put(ck, {"__miss__": True, "ts": self._now()}, overwrite=True)
            except Exception:
                pass
        return None                                   # 404 / 5xx / other -> no record

    # subclasses override lookup_by_doi / search; _normalize wraps in try/except at call sites.


# ── the six adapters ─────────────────────────────────────────────────────────────

class DoiNegotiationAdapter(_BaseAdapter):
    """DOI content negotiation (keyless, always available) — the degraded-path anchor.
    ``GET https://doi.org/{doi}`` with a CSL-JSON ``Accept`` (``CSL_ACCEPT``) -> CSL-JSON, following the
    registrar redirect (doi.org 302s to the Crossref/DataCite content-negotiation endpoint)."""

    name = "doi_negotiation"
    BASE = "https://doi.org"
    min_interval = 0.3  # ~1000 req / 5 min / IP

    def lookup_by_doi(self, doi: str) -> Optional[NormalizedRecord]:
        d = normalize_doi({"DOI": doi})
        if not d:
            return None
        raw = self._get_json(f"{self.BASE}/{d}",
                             headers={"Accept": CSL_ACCEPT, "User-Agent": USER_AGENT},
                             cache_parts=["doi", d])
        if not isinstance(raw, dict):
            return None
        try:
            return self._normalize(d, raw)
        except Exception:
            return None

    def search(self, record: dict) -> list:
        return []  # DOI content-negotiation resolves a known DOI only; no free-text search

    def _normalize(self, doi: str, csl: dict) -> Optional[NormalizedRecord]:
        # Identity comes from the AUTHORITY's payload — an empty/malformed body must NOT fabricate a
        # record that merely echoes the requested DOI. The requested DOI is attached only once the
        # payload is established as real content.
        title = _first_str(csl.get("title"))
        creators = [c for c in (_name_dict(a) for a in _as_list(csl.get("author"))) if c]
        payload_doi = normalize_doi({"DOI": csl.get("DOI")})
        if not (title or payload_doi or creators):
            return None
        d = payload_doi or normalize_doi({"DOI": doi})
        return _mk(NormalizedRecord(
            source=self.name,
            title=title,
            creators=creators,
            year=_year_from_dateparts(csl.get("issued")),
            doi=d,
            container_title=_first_str(csl.get("container-title")),
            item_type=str(csl.get("type") or ""),
            external_ids={"doi": d} if d else {},
            raw=csl,
        ))


class CrossrefAdapter(_BaseAdapter):
    """Crossref. Reuses the ``CROSSREF_URL`` anchor; ``GET /works/{doi}`` for the DOI path and
    ``GET /works?query.bibliographic=...&rows=5`` for search. Polite ``mailto`` from ``$CROSSREF_MAILTO``
    (falling back to a neutral non-placeholder default). SEARCH ranks by ``score`` DESC — ORDERING
    ONLY; the raw ``score`` is never thresholded (FR-VAL-4)."""

    name = "crossref"
    min_interval = 0.1  # polite pool ~10 req/s

    def __init__(self, *a, mailto: Optional[str] = None, **kw):
        super().__init__(*a, **kw)
        self._mailto_override = mailto

    def _mailto(self) -> str:
        return self._mailto_override or os.environ.get("CROSSREF_MAILTO") or DEFAULT_MAILTO

    def _headers(self) -> dict:
        return {"User-Agent": f"zotero-write-mcp (mailto:{self._mailto()})",
                "Accept": "application/json"}

    def _params(self, extra: Optional[dict] = None) -> dict:
        p = {"mailto": self._mailto()}
        if extra:
            p.update(extra)
        return p

    def lookup_by_doi(self, doi: str) -> Optional[NormalizedRecord]:
        d = normalize_doi({"DOI": doi})
        if not d:
            return None
        raw = self._get_json(f"{CROSSREF_BASE}/works/{d}", params=self._params(),
                             headers=self._headers(), cache_parts=["doi", d])
        msg = _as_dict(raw).get("message") if isinstance(raw, dict) else None
        if not isinstance(msg, dict):
            return None
        try:
            return self._normalize(msg)
        except Exception:
            return None

    def search(self, record: dict) -> list:
        q = _build_bib_query(record)
        if not q:
            return []
        raw = self._get_json(f"{CROSSREF_BASE}/works",
                             params=self._params({"query.bibliographic": q, "rows": 5}),
                             headers=self._headers(), cache_parts=["search", q])
        items = _as_list(_as_dict(_as_dict(raw).get("message")).get("items"))
        # Rank by Crossref relevance ``score`` DESC. This is candidate ORDERING within one query
        # ONLY — the score is a Solr/Lucene TF-IDF value, never a probability, and is NEVER compared
        # to any threshold. No candidate is dropped on score; agreement is decided later (S3 scorer).
        ranked = sorted(items, key=_score_of, reverse=True)
        out = []
        for it in ranked:
            try:
                r = self._normalize(it)
            except Exception:
                continue
            if r:
                out.append(r)
        return out

    def _normalize(self, msg: dict) -> NormalizedRecord:
        d = normalize_doi({"DOI": msg.get("DOI")})
        issued = msg.get("issued") or msg.get("published") or msg.get("published-print") \
            or msg.get("published-online")
        return _mk(NormalizedRecord(
            source=self.name,
            title=_first_str(msg.get("title")),
            creators=[c for c in (_name_dict(a) for a in _as_list(msg.get("author"))) if c],
            year=_year_from_dateparts(issued),
            doi=d,
            container_title=_first_str(msg.get("container-title")),
            item_type=str(msg.get("type") or ""),
            external_ids={"doi": d} if d else {},
            raw=msg,
        ))


class OpenAlexAdapter(_BaseAdapter):
    """OpenAlex (keyed; key required since 2026-02-13). ``available()`` reads ``OPENALEX_API_KEY``
    dynamically, so the adapter self-skips when the key is absent and self-enables when it later
    lands — no code change. The key is sent as the ``api_key`` query param and is NEVER logged or
    echoed (evidence notes and ``raw`` carry only responses, never the request key)."""

    name = "openalex"
    BASE = "https://api.openalex.org"

    def __init__(self, *a, api_key: Optional[str] = None, mailto: Optional[str] = None, **kw):
        super().__init__(*a, **kw)
        self._api_key_override = api_key   # None => read env at call time (self-enabling)
        self._mailto_override = mailto

    def _key(self) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        return os.environ.get("OPENALEX_API_KEY", "")

    def available(self) -> bool:
        return bool(self._key())

    def _unavailable_note(self) -> str:
        return f"{self.name}: unavailable (no key)"

    def _params(self, extra: Optional[dict] = None) -> dict:
        p = {"api_key": self._key()}
        mailto = self._mailto_override or os.environ.get("CROSSREF_MAILTO")
        if mailto:
            p["mailto"] = mailto
        if extra:
            p.update(extra)
        return p

    def lookup_by_doi(self, doi: str) -> Optional[NormalizedRecord]:
        if not self.available():
            return None
        d = normalize_doi({"DOI": doi})
        if not d:
            return None
        raw = self._get_json(f"{self.BASE}/works/doi:{d}", params=self._params(),
                             cache_parts=["doi", d])
        if not isinstance(raw, dict):
            return None
        try:
            return self._normalize(raw)
        except Exception:
            return None

    def search(self, record: dict) -> list:
        if not self.available():
            return []
        title = str(_as_dict(record).get("title") or "").strip()
        if not title:
            return []
        raw = self._get_json(f"{self.BASE}/works",
                             params=self._params({"search": title, "per_page": 5}),
                             cache_parts=["search", title])
        results = _as_list(_as_dict(raw).get("results"))
        out = []
        for w in results:
            try:
                r = self._normalize(w)
            except Exception:
                continue
            if r:
                out.append(r)
        return out

    def _normalize(self, w: dict) -> NormalizedRecord:
        d = normalize_doi({"DOI": w.get("doi")})
        creators = []
        for a in _as_list(w.get("authorships")):
            au = _as_dict(_as_dict(a).get("author"))
            nm = au.get("display_name") or ""
            c: dict = {"name": str(nm)} if nm else {}
            orcid = _orcid_id(au.get("orcid"))
            if orcid:
                c["orcid"] = orcid
            if c:
                creators.append(c)
        ids = _as_dict(w.get("ids"))
        ext: dict = {}
        if d:
            ext["doi"] = d
        if ids.get("openalex"):
            ext["openalex"] = ids["openalex"]
        if ids.get("pmid"):
            ext["pmid"] = ids["pmid"]
        venue = _as_dict(_as_dict(w.get("primary_location")).get("source")).get("display_name") or ""
        return _mk(NormalizedRecord(
            source=self.name,
            title=str(w.get("title") or w.get("display_name") or ""),
            creators=creators,
            year=_norm_year(w.get("publication_year")),
            doi=d,
            container_title=str(venue),
            item_type=str(w.get("type") or ""),
            external_ids=ext,
            raw=w,
        ))


class SemanticScholarAdapter(_BaseAdapter):
    """Semantic Scholar Graph API (keyless; low rate limit -> governed). ``/paper/DOI:{doi}`` for the
    DOI path and ``/paper/search/match`` for best-title match. ``/match`` 404 (no match) -> ``[]``,
    treated as empty, not an error."""

    name = "semantic_scholar"
    BASE = "https://api.semanticscholar.org"
    FIELDS = "title,year,authors,externalIds,venue,publicationTypes"
    min_interval = 1.0

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if key:
            h["x-api-key"] = key
        return h

    def lookup_by_doi(self, doi: str) -> Optional[NormalizedRecord]:
        d = normalize_doi({"DOI": doi})
        if not d:
            return None
        raw = self._get_json(f"{self.BASE}/graph/v1/paper/DOI:{d}",
                             params={"fields": self.FIELDS}, headers=self._headers(),
                             cache_parts=["doi", d])
        if not isinstance(raw, dict):
            return None
        try:
            return self._normalize(raw)
        except Exception:
            return None

    def search(self, record: dict) -> list:
        title = str(_as_dict(record).get("title") or "").strip()
        if not title:
            return []
        raw = self._get_json(f"{self.BASE}/graph/v1/paper/search/match",
                             params={"query": title, "fields": self.FIELDS},
                             headers=self._headers(), cache_parts=["match", title])
        data = _as_list(_as_dict(raw).get("data"))
        if not data:
            return []
        try:
            r = self._normalize(_as_dict(data[0]))
        except Exception:
            return []
        return [r] if r else []

    def _normalize(self, p: dict) -> NormalizedRecord:
        xi = _as_dict(p.get("externalIds"))
        d = normalize_doi({"DOI": xi.get("DOI")})
        ext: dict = {}
        if d:
            ext["doi"] = d
        if xi.get("ArXiv"):
            ext["arxiv"] = xi["ArXiv"]
        if xi.get("PubMed"):
            ext["pmid"] = xi["PubMed"]
        return _mk(NormalizedRecord(
            source=self.name,
            title=str(p.get("title") or ""),
            creators=[{"name": str(a.get("name"))} for a in _as_list(p.get("authors"))
                      if isinstance(a, dict) and a.get("name")],
            year=_norm_year(p.get("year")),
            doi=d,
            container_title=str(p.get("venue") or ""),
            item_type=_first_str(p.get("publicationTypes")),
            external_ids=ext,
            raw=p,
        ))


class DataCiteAdapter(_BaseAdapter):
    """DataCite (keyless) for data/software item types. ``GET /dois/{doi}`` and ``/dois?query=``;
    JSON:API shape (``data.attributes``). Lower priority — wired lean."""

    name = "datacite"
    BASE = "https://api.datacite.org"

    def lookup_by_doi(self, doi: str) -> Optional[NormalizedRecord]:
        d = normalize_doi({"DOI": doi})
        if not d:
            return None
        raw = self._get_json(f"{self.BASE}/dois/{d}",
                             headers={"Accept": "application/vnd.api+json"},
                             cache_parts=["doi", d])
        attrs = _as_dict(_as_dict(raw).get("data")).get("attributes")
        if not isinstance(attrs, dict):
            return None
        try:
            return self._normalize(attrs)
        except Exception:
            return None

    def search(self, record: dict) -> list:
        q = str(_as_dict(record).get("title") or "").strip()
        if not q:
            return []
        raw = self._get_json(f"{self.BASE}/dois",
                             params={"query": q, "page[size]": 5},
                             headers={"Accept": "application/vnd.api+json"},
                             cache_parts=["search", q])
        out = []
        for d in _as_list(_as_dict(raw).get("data")):
            attrs = _as_dict(d).get("attributes")
            if isinstance(attrs, dict):
                try:
                    r = self._normalize(attrs)
                except Exception:
                    continue
                if r:
                    out.append(r)
        return out

    def _normalize(self, attrs: dict) -> NormalizedRecord:
        d = normalize_doi({"DOI": attrs.get("doi")})
        titles = _as_list(attrs.get("titles"))
        title = _as_dict(titles[0]).get("title", "") if titles else ""
        creators = []
        for c in _as_list(attrs.get("creators")):
            cd = _as_dict(c)
            if cd.get("familyName") or cd.get("givenName"):
                creators.append({"firstName": str(cd.get("givenName") or ""),
                                 "lastName": str(cd.get("familyName") or "")})
            elif cd.get("name"):
                creators.append({"name": str(cd["name"])})
        return _mk(NormalizedRecord(
            source=self.name,
            title=str(title),
            creators=creators,
            year=_norm_year(attrs.get("publicationYear")),
            doi=d,
            container_title=str(attrs.get("publisher") or ""),
            item_type=str(_as_dict(attrs.get("types")).get("resourceTypeGeneral") or ""),
            external_ids={"doi": d} if d else {},
            raw=attrs,
        ))


class OrcidAdapter(_BaseAdapter):
    """ORCID — evidence-only author-ID overlap; NEVER auto-resolves identity and NEVER drives any
    accept/reject (there is no gate in this module anyway). Not a bibliographic candidate source:
    ``lookup_by_doi`` and ``search`` return nothing; ``author_orcids`` surfaces the ORCID iDs already
    present on a record (network-free), and ``lookup_orcid`` fetches a public profile as evidence."""

    name = "orcid"
    BASE = "https://pub.orcid.org/v3.0"

    def lookup_by_doi(self, doi: str) -> Optional[NormalizedRecord]:
        return None  # ORCID is not DOI-addressable

    def search(self, record: dict) -> list:
        return []    # ORCID is not a bibliographic candidate source

    def author_orcids(self, record: Any) -> list:
        """Network-free: the ORCID iDs present on a NormalizedRecord (or dict) — evidence only."""
        if isinstance(record, NormalizedRecord):
            creators, ext = record.creators, record.external_ids
        else:
            rd = _as_dict(record)
            creators, ext = _as_list(rd.get("creators")), _as_dict(rd.get("external_ids"))
        out = []
        for c in creators:
            oid = _orcid_id(_as_dict(c).get("orcid"))
            if oid:
                out.append(oid)
        if ext.get("orcid"):
            out.append(_orcid_id(ext["orcid"]))
        return list(dict.fromkeys([o for o in out if o]))  # dedup, preserve order

    def lookup_orcid(self, orcid: str) -> Optional[dict]:
        """Fetch a public ORCID profile as an evidence dict ``{orcid, name}`` (never a decision)."""
        oid = _orcid_id(orcid)
        if not oid:
            return None
        raw = self._get_json(f"{self.BASE}/{oid}/person",
                             headers={"Accept": "application/json"}, cache_parts=[oid])
        if not isinstance(raw, dict):
            return None
        try:
            nm = _as_dict(raw.get("name"))
            family = _as_dict(nm.get("family-name")).get("value") or ""
            given = _as_dict(nm.get("given-names")).get("value") or ""
            return {"orcid": oid, "name": " ".join(x for x in (given, family) if x).strip()}
        except Exception:
            return {"orcid": oid, "name": ""}


# ── query building + composition over available authorities (degraded path) ──────

def _build_bib_query(record: Any) -> str:
    """Free-text bibliographic query from a candidate record: ``<title> <first-author> <year>``.
    Uses the reused dedup normalizers for author/year (drift-free); the title is passed verbatim."""
    from .dedup import first_author_surname
    r = _as_dict(record)
    # Sanitize creators to a list of dicts BEFORE the reused normalizer: first_author_surname calls
    # c.get(...) per creator with no type guard, so a hostile creators value (a str, or a list of
    # non-dicts) would raise AttributeError — violating the adapter "never raises" contract at the
    # external-input boundary. Mirrors the guard OrcidAdapter.author_orcids already applies.
    safe = {**r, "creators": [c for c in _as_list(r.get("creators")) if isinstance(c, dict)]}
    title = str(safe.get("title") or "").strip()
    author = first_author_surname(safe)
    year = normalize_year(safe)
    return " ".join(x for x in (title, author, year) if x).strip()


def default_authorities(*, transport: Any = None, cache: Optional[JsonCache] = None,
                        cache_root: "os.PathLike[str] | str | None" = None,
                        mailto: Optional[str] = None,
                        sleep: Optional[Callable[[float], None]] = None,
                        monotonic: Optional[Callable[[], float]] = None,
                        now: Optional[Callable[[], float]] = None,
                        miss_ttl: float = 86400.0) -> list:
    """Build the six adapters sharing one cache + injected clocks. The default authority set for
    both the S3 validation scorer and the S4 ingest join."""
    if cache is None:
        cache = JsonCache(cache_root if cache_root is not None else default_cache_root())
    common = {"transport": transport, "cache": cache, "sleep": sleep, "monotonic": monotonic,
              "now": now, "miss_ttl": miss_ttl}
    return [
        DoiNegotiationAdapter(**common),
        CrossrefAdapter(mailto=mailto, **common),
        OpenAlexAdapter(mailto=mailto, **common),
        SemanticScholarAdapter(**common),
        DataCiteAdapter(**common),
        OrcidAdapter(**common),
    ]


@dataclass
class AuthorityResult:
    """The honest, read-only outcome of composing over authorities: the records gathered, an
    ``evidence`` note per authority (including ``"<name>: unavailable ..."`` for degraded ones), and
    which authorities were available / answered. This surfaces availability honestly; the "too few
    authorities answered -> flag" DECISION belongs to S3, not here."""

    records: list = field(default_factory=list)      # list[NormalizedRecord]
    evidence: list = field(default_factory=list)     # list[str]
    available: list = field(default_factory=list)    # names with available() True
    answered: list = field(default_factory=list)     # names that returned >=1 record


def gather_by_doi(doi: str, authorities: list) -> AuthorityResult:
    """Resolve ``doi`` across the *available* authorities, never raising. A keyless/down/erroring
    authority contributes an evidence note (never an exception that aborts the batch)."""
    res = AuthorityResult()
    for a in authorities:
        try:
            ok = a.available()
        except Exception:
            ok = False
        if not ok:
            res.evidence.append(_adapter_note(a, "unavailable"))
            continue
        res.available.append(a.name)
        try:
            rec = a.lookup_by_doi(doi)
        except Exception as e:  # adapters shouldn't raise, but never let one abort the batch
            res.evidence.append(f"{a.name}: error ({type(e).__name__})")
            continue
        if rec is None:
            res.evidence.append(f"{a.name}: no record")
        else:
            res.records.append(rec)
            res.answered.append(a.name)
            res.evidence.append(f"{a.name}: ok")
    return res


def gather_by_search(record: dict, authorities: list) -> AuthorityResult:
    """Candidate search across the *available* authorities (title/author/year), never raising.
    Returns all normalized candidates + per-authority evidence. No thresholding, no scoring — S3
    ranks/decides. (Crossref candidates arrive score-ordered; the score itself never crosses a bar.)"""
    res = AuthorityResult()
    for a in authorities:
        try:
            ok = a.available()
        except Exception:
            ok = False
        if not ok:
            res.evidence.append(_adapter_note(a, "unavailable"))
            continue
        res.available.append(a.name)
        try:
            cands = a.search(record) or []
        except Exception as e:
            res.evidence.append(f"{a.name}: error ({type(e).__name__})")
            continue
        if cands:
            res.records.extend(cands)
            res.answered.append(a.name)
        res.evidence.append(f"{a.name}: {len(cands)} candidate(s)")
    return res


def _adapter_note(adapter: Any, kind: str) -> str:
    if kind == "unavailable":
        fn = getattr(adapter, "_unavailable_note", None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
        return f"{getattr(adapter, 'name', 'authority')}: unavailable"
    return f"{getattr(adapter, 'name', 'authority')}: {kind}"
