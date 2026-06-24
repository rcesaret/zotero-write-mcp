"""Duplicate scanning (Phase 2): deterministic ASySD auto-accept + a probabilistic review-queue seam.

INV-DEDUP / ADR-005: ``auto_accept`` fires ONLY on the deterministic ASySD boolean — an exact shared DOI
OR an exact shared normalized (title + year + first-author) — with item-type + DOI-conflict guards.
Different item types are NEVER auto-merged. Splink probabilistic scores feed the HUMAN-REVIEW QUEUE ONLY
(Stage-E H-3 forbids a probabilistic score from driving a destructive merge); that path needs
datasketch/Splink/DuckDB (Q5) + labeled-set calibration (Q4) and is a clearly-marked SEAM here, not built.

`dedup_scan` itself NEVER commits — it only identifies candidate clusters; the maintenance-runner feeds an
``auto_accept`` cluster through snapshot -> merge_cluster -> commit_merge, which re-gates on the 11-check
verify + the enable token. The normalizer (Stage-E H-2) is FROZEN + adversarially unit-tested: an
over-aggressive normalizer would let a probabilistic decision wear a deterministic mask.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from zotero_write_mcp.merge import _NON_SCALAR_FIELDS

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_YEAR = re.compile(r"\b(1[0-9]{3}|2[0-9]{3})\b")


def _data(item: Any) -> dict:
    return item.get("data", item)


def _strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_title(title: Any) -> str:
    """FROZEN normalizer: drop a trailing subtitle after the FIRST colon, strip diacritics + punctuation,
    lowercase, collapse whitespace. NOTE (H-2): the subtitle-drop is deliberately conservative — two works
    sharing a main title but differing only in subtitle WILL share this key, so the auto-accept path
    additionally requires year + first-author to match AND no conflicting DOI (see ``dedup_scan``)."""
    if not title:
        return ""
    t = str(title).split(":", 1)[0]
    t = _PUNCT.sub(" ", _strip_diacritics(t).lower())
    return _WS.sub(" ", t).strip()


_STOPWORDS = frozenset(
    "a an the of and or in on for to with by from as at into over under after before is are".split())
TITLE_JACCARD_FLOOR = 0.5   # below this on FULL-title content tokens -> different works -> demote to review


def normalize_title_full(title: Any) -> str:
    """Full normalized title WITHOUT dropping the subtitle — the discriminator for the auto-accept paths
    (review B2 / DEDUP-3): two works sharing a subtitle-stripped key, or a coincidentally-shared DOI, but
    differing in their full titles must NOT auto-merge."""
    if not title:
        return ""
    return _WS.sub(" ", _PUNCT.sub(" ", _strip_diacritics(str(title)).lower())).strip()


def _content_tokens(title_norm: str) -> set:
    return {t for t in title_norm.split() if t not in _STOPWORDS}


def _min_title_jaccard(by_key: dict, keys: list) -> float:
    """Minimum pairwise Jaccard over FULL-title content tokens (stopwords stripped). 1.0 if < 2 titles —
    a single shared stopword no longer defeats the guard (DEDUP-3)."""
    sets = [s for s in (_content_tokens(normalize_title_full(_data(by_key[k]).get("title"))) for k in keys) if s]
    if len(sets) < 2:
        return 1.0
    worst = 1.0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            worst = min(worst, (len(sets[i] & sets[j]) / len(union)) if union else 1.0)
    return worst


def normalize_year(item: Any) -> str:
    d = _data(item)
    for fld in ("date", "year", "issued"):
        v = d.get(fld)
        if v:
            m = _YEAR.search(str(v))
            if m:
                return m.group(1)
    return ""


def first_author_surname(item: Any) -> str:
    for c in _data(item).get("creators", []) or []:
        if c.get("creatorType") in (None, "author"):
            ln = c.get("lastName") or c.get("name") or ""
            if ln:
                return _WS.sub(" ", _strip_diacritics(str(ln)).lower()).strip()
    return ""


def normalize_doi(item: Any) -> Optional[str]:
    doi = str(_data(item).get("DOI") or _data(item).get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi or None


def asysd_key(item: Any) -> Optional[tuple]:
    """The deterministic normalized key ``(title, year, first-author)``. Returns None when title or year
    is missing — an incomplete record never deterministically auto-accepts."""
    t = normalize_title(_data(item).get("title"))
    y = normalize_year(item)
    if not t or not y:
        return None
    return (t, y, first_author_surname(item))


def _item_type(item: Any) -> Any:
    return _data(item).get("itemType")


def _completeness(item: Any) -> int:
    """Count non-empty BIBLIOGRAPHIC scalar fields only (DEDUP-2): collections/tags/relations/timestamps/
    version/key are excluded so a sparse record that merely sits in more collections cannot outrank a
    metadata-rich one for master selection."""
    return sum(1 for k, v in _data(item).items()
               if k not in _NON_SCALAR_FIELDS and v not in (None, "", [], {}))


def select_master(by_key: dict, keys: list) -> str:
    """H-6: deterministic master selection — the most-complete record; tiebreak the lexicographically
    lowest key (stable + reproducible)."""
    return sorted(keys, key=lambda k: (-_completeness(by_key[k]), k))[0]


@dataclass
class Candidate:
    item_keys: list
    auto_accept: bool
    reason: str
    master_key: Optional[str] = None
    conflicts: list = field(default_factory=list)


def dedup_scan(items: list, *, review_threshold: float = 0.99) -> dict:
    """Deterministic auto-accept clustering over a list of Zotero item dicts. Returns
    ``{candidate_clusters, auto_accept_count, review_count, probabilistic_review}``.

    ``auto_accept`` fires ONLY on (a) an exact shared DOI with no item-type/title/year conflict, or (b) an
    exact shared ASySD normalized key (title+year+author), a single item type, AND no conflicting DOI.
    Anything else is demoted to the human-review queue. Different item types never auto-merge."""
    by_key: dict = {}
    for it in items:
        by_key[_data(it).get("key") or it.get("key")] = it

    clusters: list = []
    claimed: set = set()

    # (a) group by exact DOI
    doi_groups: dict = {}
    for k, it in by_key.items():
        doi = normalize_doi(it)
        if doi:
            doi_groups.setdefault(doi, []).append(k)
    for doi, keys in doi_groups.items():
        if len(keys) < 2:
            continue
        conflicts = _conflicts(by_key, keys, path="doi")        # same DOI; demote on type/year/GROSS-title
        clusters.append(Candidate(
            item_keys=sorted(keys), auto_accept=not conflicts,
            reason=f"exact DOI {doi}" + (f" (demoted: {', '.join(conflicts)})" if conflicts else ""),
            master_key=select_master(by_key, keys), conflicts=conflicts))
        claimed.update(keys)

    # (b) group by exact ASySD normalized key (items not already DOI-clustered)
    key_groups: dict = {}
    for k, it in by_key.items():
        if k in claimed:
            continue
        ak = asysd_key(it)
        if ak:
            key_groups.setdefault(ak, []).append(k)
    for _ak, keys in key_groups.items():
        if len(keys) < 2:
            continue
        conflicts = _conflicts(by_key, keys, path="key")        # H-2: differing DOIs -> different works
        clusters.append(Candidate(
            item_keys=sorted(keys), auto_accept=not conflicts,
            reason="exact normalized title+year+author"
                   + (f" (demoted: {', '.join(conflicts)})" if conflicts else ""),
            master_key=select_master(by_key, keys), conflicts=conflicts))
        claimed.update(keys)

    auto = [c for c in clusters if c.auto_accept]
    return {
        "candidate_clusters": clusters,
        "auto_accept_count": len(auto),
        "review_count": len(clusters) - len(auto),
        # SEAM — probabilistic near-duplicate detection (MinHash/LSH pre-block -> Splink Fellegi-Sunter):
        # review-queue ONLY (never auto-commit). Needs datasketch/Splink/DuckDB (Q5) + labeled calibration
        # (Q4, review_threshold default 0.99). Not built here; deterministic auto-accept is the only auto path.
        "probabilistic_review": {"enabled": False, "review_threshold": review_threshold,
                                 "reason": "Q4 calibration + Q5 infra required"},
    }


def _conflicts(by_key: dict, keys: list, *, path: str) -> list:
    """Hard conflicts that DEMOTE an otherwise-deterministic cluster to human review.

    BOTH paths apply a FULL-title content-token Jaccard floor (review B2 + DEDUP-3): the subtitle-drop in
    the normalized key, and the old zero-token-overlap gross check, were too weak — two genuinely different
    works could share a DOI (typo/reuse) or a subtitle-stripped key and auto-merge. ``path="doi"``
    additionally demotes on a year disagreement; ``path="key"`` on a conflicting non-null DOI."""
    out: list = []
    if len({_item_type(by_key[k]) for k in keys}) > 1:
        out.append("item-type conflict")
    if _min_title_jaccard(by_key, keys) < TITLE_JACCARD_FLOOR:
        out.append("title disagreement")
    if path == "doi":
        years = {normalize_year(by_key[k]) for k in keys}
        years.discard("")
        if len(years) > 1:
            out.append("year disagreement")
    elif path == "key":
        dois = {normalize_doi(by_key[k]) for k in keys}
        dois.discard(None)
        if len(dois) > 1:
            out.append("DOI disagreement")
    return out
