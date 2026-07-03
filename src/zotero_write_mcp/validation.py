"""Deterministic field-agreement scorer + PINNED 3-way accept/flag/reject gate (Phase-3, sprint S3).

Consumes the S-V1 read-only source clients (:mod:`zotero_write_mcp.sources`) and turns a candidate
record + the ``NormalizedRecord``s external authorities returned into a calibrated decision. This
module is PURE LOGIC — no I/O, no network, no Zotero writes, no LLM confidence input. It is the single
shared confidence primitive S3-cal (Platt fit) and S4 (ingest auto-create) bind to; keep its public
shapes stable.

Two invariants hold everywhere in this module (INV-COMP, ADR-005; PLAN1 SS1.4/1.6):
  1. Confidence is agreement-based, NEVER LLM self-report. ``p``/``decision`` are computed by
     deterministic code over cross-source agreement; nothing here reads a model-supplied "confidence"
     field on the input record.
  2. The 3-way gate STRUCTURE is PINNED (parameter-registry "v1.1 validation + ingest floors"); only
     the Platt calibration mapping ``p_raw -> p`` is meant to change (S3-cal). The conflict-override
     and the accept AND-clause are hard structural checks here, not learnable weights.

Reuses the dedup normalizers (``normalize_title``/``normalize_year``/``first_author_surname``/
``normalize_doi``) and ``utils.title_similarity`` verbatim — no drift between the scorer, the merge
dedup path, and the HMAC approval-token identity (below).
"""
from __future__ import annotations

import hmac
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .dedup import first_author_surname, normalize_doi, normalize_title, normalize_year

# NOTE: `.utils` eagerly imports httpx + bibtexparser at module level (same issue sources.py's
# docstring documents for `utils.resolve_doi`), which would defeat offline-clean-import for callers
# that only need the identity/HMAC-token helpers (the harness hooks run under a bare interpreter with
# no httpx installed). `title_similarity` is therefore imported LAZILY, inside the two functions that
# actually need it (_title_field/_venue_field) — every other function in this module, including the
# whole identity/HMAC-token surface the hooks call, stays httpx-free at import time.

# ── PINNED constants (parameter-registry "v1.1 validation + ingest floors") ──────────────────────
# Field weights sum to exactly 1.0. Retune ONLY on labeled data (S3-cal); do not hand-edit here.
WEIGHT_TITLE = 0.55
WEIGHT_AUTHOR = 0.20
WEIGHT_YEAR = 0.10
WEIGHT_VENUE = 0.10
WEIGHT_ID = 0.05

ACCEPT_P_FLOOR = 0.90       # PINNED structure: p >= floor AND (id_agreement OR consensus) AND no conflicts
REJECT_P_CEILING = 0.55     # PINNED structure: p < ceiling AND no identifier agreement AND no conflicts

# Year agreement: exact match = 1.0; +/-1 (print-vs-online drift) = partial credit; else 0.0.
YEAR_OFFBY1_CREDIT = 0.5


# ── candidate/authority record access helpers (accept either a dict or a NormalizedRecord) ───────

def _rec_dict(r: Any) -> dict:
    """Normalize a candidate/authority record to a plain dict. Accepts a raw dict OR a
    ``sources.NormalizedRecord`` (has ``.as_dict()``); never mutates the input."""
    if r is None:
        return {}
    as_dict = getattr(r, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return r if isinstance(r, dict) else {}


def _candidate_title(rec: dict) -> str:
    return str(rec.get("title") or "")


def _candidate_venue(rec: dict) -> str:
    """First non-empty of the Zotero/CSL venue-shaped fields, checked in priority order."""
    for k in ("container_title", "publicationTitle", "bookTitle", "proceedingsTitle", "series"):
        v = rec.get(k)
        if v:
            return str(v)
    return ""


def _candidate_item_type(rec: dict) -> str:
    return str(rec.get("item_type") or rec.get("itemType") or "").strip().lower()


def _candidate_doi(rec: dict) -> Optional[str]:
    return normalize_doi({"DOI": rec.get("doi") if "doi" in rec else rec.get("DOI")})


def _candidate_year(rec: dict) -> str:
    return normalize_year({"date": rec.get("date") or rec.get("year") or rec.get("issued") or ""})


def _candidate_first_author(rec: dict) -> str:
    return first_author_surname({"creators": rec.get("creators") or []})


def _candidate_orcids(rec: dict) -> frozenset:
    out = set()
    for c in rec.get("creators") or []:
        if isinstance(c, dict) and c.get("orcid"):
            out.add(str(c["orcid"]).strip().upper())
    ext = rec.get("external_ids") or {}
    if isinstance(ext, dict) and ext.get("orcid"):
        out.add(str(ext["orcid"]).strip().upper())
    return frozenset(out)


# ── item-type coarse family (heuristic; used only for the item_type_mismatch conflict) ────────────
_ITEM_TYPE_FAMILY = {
    "journalarticle": "article", "journal-article": "article", "article-journal": "article",
    "article": "article", "magazinearticle": "article", "newspaperarticle": "article",
    "book": "book", "monograph": "book",
    "booksection": "chapter", "chapter": "chapter", "book-chapter": "chapter",
    "conferencepaper": "conference", "proceedings-article": "conference", "paper-conference": "conference",
    "thesis": "thesis", "dissertation": "thesis",
    "report": "report", "report-document": "report",
    "dataset": "dataset",
}


def _item_type_family(item_type: str) -> str:
    return _ITEM_TYPE_FAMILY.get(item_type, item_type)


# ── structured outputs ─────────────────────────────────────────────────────────────────────────

@dataclass
class Conflict:
    """A single agreement conflict. ``kind`` drives the gate:
    ``"id_disagreement"`` (identifier disagreement — CONFLICT-OVERRIDE, always -> flag),
    ``"doi_unresolved"`` (a DOI was given but no authority could resolve it),
    ``"item_type_mismatch"`` (candidate and a strong-title-match authority disagree on work type)."""

    kind: str
    detail: str
    values: list = field(default_factory=list)
    authorities: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "values": self.values,
                "authorities": self.authorities}


def _conflict_kind(c: Any) -> Optional[str]:
    if isinstance(c, Conflict):
        return c.kind
    if isinstance(c, dict):
        return c.get("kind")
    return None


@dataclass
class GateEvidence:
    """The structured evidence :func:`decide` consumes alongside ``p`` and ``conflicts``.
    ``id_agreement``: the candidate's OWN identifier (DOI) exactly matches >=1 authority.
    ``consensus``: >=2 DISTINCT authorities independently agree with EACH OTHER on the same DOI
    (meaningful even when the candidate carries no DOI at all — the ingest use case)."""

    id_agreement: bool = False
    consensus: bool = False
    notes: list = field(default_factory=list)
    # True iff >=1 authority record was actually available to compare against. A record with ZERO
    # authority answers has NO evidence either way (not a confident "this is wrong") — decide() must
    # not let that degenerate zero-score state read as "reject"; it must fall through to "flag".
    evidence_gathered: bool = True


@dataclass
class ScoreResult:
    p_raw: float
    per_field: dict
    consensus: bool
    consensus_count: int
    id_agreement: bool
    conflicts: list           # list[Conflict]
    evidence: list            # list[str] — human-readable notes (authority answers, ORCID overlap, ...)
    n_authorities: int = 0

    def gate_evidence(self) -> GateEvidence:
        return GateEvidence(id_agreement=self.id_agreement, consensus=self.consensus,
                             notes=list(self.evidence), evidence_gathered=self.n_authorities > 0)


# ── per-field agreement (each in [0,1]; reuses the dedup normalizers + title_similarity) ─────────

def _title_field(rec: dict, authorities: list) -> float:
    from .utils import title_similarity
    cand = _candidate_title(rec)
    if not cand or not authorities:
        return 0.0
    return max((title_similarity(cand, _candidate_title(a)) for a in authorities), default=0.0)


def _author_field(rec: dict, authorities: list) -> tuple:
    """Returns (score, evidence_notes). ORCID overlap is evidence-only (never decisive — it never
    changes the returned score), per PLAN1 SS1.4."""
    cand = _candidate_first_author(rec)
    notes = []
    score = 0.0
    if cand:
        for a in authorities:
            if _candidate_first_author(a) == cand:
                score = 1.0
                break
    cand_orcids = _candidate_orcids(rec)
    if cand_orcids:
        for a in authorities:
            overlap = cand_orcids & _candidate_orcids(a)
            if overlap:
                notes.append(f"orcid overlap with {a.get('source', 'authority')}: {sorted(overlap)} "
                             f"(evidence-only, not scored)")
    return score, notes


def _year_field(rec: dict, authorities: list) -> float:
    cand = _candidate_year(rec)
    if not cand or not authorities:
        return 0.0
    best = 0.0
    try:
        cand_i = int(cand)
    except ValueError:
        return 0.0
    for a in authorities:
        ay = _candidate_year(a)
        if not ay:
            continue
        if ay == cand:
            return 1.0
        try:
            if abs(int(ay) - cand_i) == 1:
                best = max(best, YEAR_OFFBY1_CREDIT)
        except ValueError:
            continue
    return best


def _venue_field(rec: dict, authorities: list) -> float:
    from .utils import title_similarity
    cand = _candidate_venue(rec)
    if not cand or not authorities:
        return 0.0
    return max((title_similarity(cand, _candidate_venue(a)) for a in authorities), default=0.0)


def _id_field(rec: dict, authorities: list) -> tuple:
    """Returns (score, id_agreement). id_agreement = the candidate's OWN doi exactly matches >=1
    authority's doi. If the candidate has no DOI, id_agreement is always False (nothing to agree on)."""
    cand = _candidate_doi(rec)
    if not cand:
        return 0.0, False
    for a in authorities:
        if _candidate_doi(a) == cand:
            return 1.0, True
    return 0.0, False


def _consensus(authorities: list) -> tuple:
    """>=2 DISTINCT authorities independently returning the SAME non-null DOI. Independent of the
    candidate's own DOI (or lack of one) — this is authority-vs-authority corroboration."""
    counts: dict = {}
    owners: dict = {}
    for a in authorities:
        d = _candidate_doi(a)
        if not d:
            continue
        counts[d] = counts.get(d, 0) + 1
        owners.setdefault(d, []).append(a.get("source") or "authority")
    if not counts:
        return False, 0, None
    best_doi, best_n = max(counts.items(), key=lambda kv: kv[1])
    return best_n >= 2, best_n, (best_doi if best_n >= 2 else None)


def _id_disagreement(rec: dict, authorities: list) -> Optional[Conflict]:
    """>=2 DISTINCT non-null DOIs across {candidate's own DOI (if any)} UNION {each authority's DOI}
    -> a real identifier disagreement (authority-vs-authority OR candidate-vs-authority)."""
    seen: dict = {}   # doi -> [source labels]
    cand_doi = _candidate_doi(rec)
    if cand_doi:
        seen.setdefault(cand_doi, []).append("candidate")
    for a in authorities:
        d = _candidate_doi(a)
        if d:
            seen.setdefault(d, []).append(a.get("source") or "authority")
    if len(seen) < 2:
        return None
    values = sorted(seen.keys())
    srcs = sorted({s for v in seen.values() for s in v})
    return Conflict(kind="id_disagreement",
                     detail=f"{len(values)} distinct DOIs across {srcs}", values=values,
                     authorities=srcs)


def _item_type_mismatch(rec: dict, authorities: list) -> Optional[Conflict]:
    """A strong-title-match authority (title_similarity >= 0.85) whose item-type FAMILY differs from
    the candidate's -> likely a different kind of work wearing a similar title. Heuristic family
    mapping (_ITEM_TYPE_FAMILY); unmapped types compare as themselves."""
    from .utils import title_similarity
    cand_type = _candidate_item_type(rec)
    if not cand_type:
        return None
    cand_family = _item_type_family(cand_type)
    cand_title = _candidate_title(rec)
    for a in authorities:
        a_type = _candidate_item_type(a)
        if not a_type:
            continue
        a_family = _item_type_family(a_type)
        if a_family == cand_family:
            continue
        if cand_title and title_similarity(cand_title, _candidate_title(a)) >= 0.85:
            return Conflict(kind="item_type_mismatch",
                             detail=f"candidate itemType {cand_type!r} ({cand_family}) vs "
                                    f"{a.get('source')} {a_type!r} ({a_family}) on a near-identical title",
                             values=[cand_type, a_type], authorities=[a.get("source") or "authority"])
    return None


def _doi_unresolved(rec: dict, authorities: list, *, doi_lookup_attempted: bool) -> Optional[Conflict]:
    cand_doi = _candidate_doi(rec)
    if not (cand_doi and doi_lookup_attempted and not authorities):
        return None
    return Conflict(kind="doi_unresolved", detail=f"DOI {cand_doi!r} resolved by no authority",
                     values=[cand_doi], authorities=[])


# ── the scorer ──────────────────────────────────────────────────────────────────────────────────

def score_record(record: Any, authority_records: list, *, doi_lookup_attempted: bool = False) -> ScoreResult:
    """Pure field-agreement scorer. ``record`` is the candidate (dict, Zotero-native field names);
    ``authority_records`` is the list of ``NormalizedRecord`` (or dicts) external authorities returned
    (e.g. from ``sources.gather_by_doi``/``gather_by_search``). Deterministic; makes no network calls;
    reads no model-supplied confidence field on ``record`` (INV-COMP)."""
    rec = _rec_dict(record)
    auths = [_rec_dict(a) for a in (authority_records or [])]

    title_s = _title_field(rec, auths)
    author_s, author_notes = _author_field(rec, auths)
    year_s = _year_field(rec, auths)
    venue_s = _venue_field(rec, auths)
    id_s, id_agreement = _id_field(rec, auths)

    p_raw = (WEIGHT_TITLE * title_s + WEIGHT_AUTHOR * author_s + WEIGHT_YEAR * year_s
             + WEIGHT_VENUE * venue_s + WEIGHT_ID * id_s)
    p_raw = max(0.0, min(1.0, p_raw))

    consensus, consensus_count, consensus_doi = _consensus(auths)

    conflicts = []
    idd = _id_disagreement(rec, auths)
    if idd:
        conflicts.append(idd)
    du = _doi_unresolved(rec, auths, doi_lookup_attempted=doi_lookup_attempted)
    if du:
        conflicts.append(du)
    itm = _item_type_mismatch(rec, auths)
    if itm:
        conflicts.append(itm)

    evidence = [f"title={title_s:.3f} author={author_s:.3f} year={year_s:.3f} "
                f"venue={venue_s:.3f} id={id_s:.3f} -> p_raw={p_raw:.3f}"]
    evidence.extend(author_notes)
    if consensus:
        evidence.append(f"consensus: {consensus_count} authorities agree on DOI {consensus_doi!r}")
    if not auths:
        evidence.append("no authority records available")

    return ScoreResult(
        p_raw=p_raw,
        per_field={"title": title_s, "author": author_s, "year": year_s, "venue": venue_s, "id": id_s},
        consensus=consensus, consensus_count=consensus_count, id_agreement=id_agreement,
        conflicts=conflicts, evidence=evidence, n_authorities=len(auths),
    )


# ── the PINNED 3-way gate ──────────────────────────────────────────────────────────────────────

def decide(p: float, evidence: GateEvidence, conflicts: list) -> str:
    """The PINNED 3-way gate. Returns ``"accept"`` | ``"flag"`` | ``"reject"``.

    Evaluation order (first match wins — see PLAN1 SS1.6 / parameter-registry "v1.1 validation +
    ingest floors — PINNED"):

      1. CONFLICT OVERRIDE (guard clause, evaluated FIRST, unconditionally): any identifier
         disagreement -> ``"flag"``, regardless of ``p`` — even ``p=0.99``. This is a guard clause
         that returns before any accept/reject variable is computed, so no later refactor of the
         p-band logic can let a high-p conflicting record fall through to accept.
      2. accept: ``p >= ACCEPT_P_FLOOR`` AND (``evidence.id_agreement`` OR ``evidence.consensus``)
         AND no conflicts at all (any conflict kind, not just id_disagreement, blocks accept).
      3. reject: ``p < REJECT_P_CEILING`` AND no identifier agreement AND no conflicts.
      4. flag: everything else (the fail-toward-flag catch-all).
    """
    # ROW 1 — CONFLICT OVERRIDE. Structurally first: nothing below this line can run for an
    # id-disagreement record, so a p-band change can never accidentally let one reach "accept".
    if any(_conflict_kind(c) == "id_disagreement" for c in (conflicts or [])):
        return "flag"

    has_conflicts = bool(conflicts)

    # ROW 2 — accept: hard structural AND-clause (not learnable by p alone).
    if p >= ACCEPT_P_FLOOR and not has_conflicts and (evidence.id_agreement or evidence.consensus):
        return "accept"

    # ROW 3 — reject: only when clearly wrong AND no identifier agreement AND no conflicts AND we
    # actually gathered evidence to be wrong about (zero authority answers is "unknown", not "wrong").
    if (p < REJECT_P_CEILING and not evidence.id_agreement and not has_conflicts
            and evidence.evidence_gathered):
        return "reject"

    # ROW 4 — flag: the catch-all (mid-band, any conflict, or missing/ambiguous evidence).
    return "flag"


# ── calibration (cold-start conservative floor; Platt fit is S3-cal) ─────────────────────────────

DEFAULT_CALIBRATION_PATH = Path("runtime") / "validation-calibration.json"

# In-code fallback used when the JSON file is absent/unreadable — NEVER silently widens the accept
# band; this IS the conservative cold-start floor, not merely a placeholder. See the JSON file's own
# "notes" field for the full worked-example derivation.
DEFAULT_CALIBRATION = {
    "calibration_version": "cold-start-v1",
    "n_labeled": 0,
    "fit_date": None,
    "accept_band_precision": None,
    "method": "conservative-floor",
    "platt": {"A": 1.0, "B": -6.0},
    "consensus_floor": 0.92,
    "consensus_min_p_raw": 0.5,
    "notes": (
        "Cold-start conservative floor (no owner labels yet). Platt(A=1.0,B=-6.0) alone maps even a "
        "perfect field match (p_raw=1.0) to sigmoid(1.0-6.0)~=0.0067 -- far below the 0.90 accept "
        "floor, so single-authority id_agreement NEVER reaches accept at cold start. Only when >=2 "
        "authorities independently corroborate the SAME DOI (consensus=True) AND field agreement is "
        "at least middling (p_raw>=consensus_min_p_raw) does calibrated p get floored up to "
        "consensus_floor=0.92 (just above the 0.90 gate) -- 'the accept band is effectively "
        "unreachable except on exact multi-authority DOI consensus' (PLAN1 SS1.5)."
    ),
}


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def load_calibration(path: "os.PathLike[str] | str | None" = None) -> dict:
    """Load the calibration JSON; fall back to :data:`DEFAULT_CALIBRATION` on ANY absence/error
    (missing file, malformed JSON, missing keys) — never crash, never silently widen the accept band."""
    p = Path(path) if path is not None else DEFAULT_CALIBRATION_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "platt" not in data:
            return dict(DEFAULT_CALIBRATION)
        merged = dict(DEFAULT_CALIBRATION)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_CALIBRATION)


def apply_calibration(p_raw: float, consensus: bool, calibration: dict) -> float:
    """Map ``p_raw`` (+ the consensus feature) -> calibrated ``p`` via the loaded calibration.
    Consensus is encoded as a SEPARATE input (a floor bump), never hand-blended into ``p_raw`` itself
    (PLAN1 SS1.4: "encode as a separate feature/input to calibration, not a hand-tuned addition"),
    which keeps it learnable for the S3-cal Platt refit."""
    platt = calibration.get("platt") or DEFAULT_CALIBRATION["platt"]
    A, B = float(platt.get("A", 1.0)), float(platt.get("B", -6.0))
    p = _sigmoid(A * p_raw + B)
    if consensus and p_raw >= float(calibration.get("consensus_min_p_raw", 0.5)):
        p = max(p, float(calibration.get("consensus_floor", 0.92)))
    return max(0.0, min(1.0, p))


# ── validate_record composition (score -> calibrate -> gate) ─────────────────────────────────────

def build_validation_result(record: Any, authority_records: list, calibration: dict, *,
                             doi_lookup_attempted: bool = False,
                             extra_evidence: Optional[list] = None) -> dict:
    """Compose :func:`score_record` -> :func:`apply_calibration` -> :func:`decide` into the exact
    ``{p, decision, evidence, conflicts}`` shape (TC-7). Pure; the caller (the ``validate_record``
    MCP tool) owns all I/O (authority gathering, PROV logging)."""
    result = score_record(record, authority_records, doi_lookup_attempted=doi_lookup_attempted)
    p = apply_calibration(result.p_raw, result.consensus, calibration)
    ge = result.gate_evidence()
    decision = decide(p, ge, result.conflicts)
    evidence = list(result.evidence) + list(extra_evidence or [])
    return {
        "p": p,
        "p_raw": result.p_raw,
        "decision": decision,
        "evidence": evidence,
        "conflicts": [c.as_dict() if isinstance(c, Conflict) else c for c in result.conflicts],
        "per_field": result.per_field,
        "consensus": result.consensus,
        "consensus_count": result.consensus_count,
        "id_agreement": result.id_agreement,
        "calibration_version": calibration.get("calibration_version"),
    }


# ── un-spoofable HMAC approval token (mirrors merge_live.py's ZOT_MERGE_LIVE_ENABLED doctrine) ───
#
# The identity string is built from the SAME reused normalizers as the scorer, so the minter
# (scripts/approve_record.py) and the validation-gate hook derive BYTE-FOR-BYTE identical input as
# long as both import this function (no separate reimplementation, no drift risk).

APPROVAL_HMAC_KEY_ENV = "ZOT_APPROVAL_HMAC_KEY"
_IDENTITY_DELIM = "|"


def normalized_identity(record: Any) -> tuple:
    """The 5-tuple ``(itemType, title, year, firstAuthor, DOI)``, each run through the SAME reused
    normalizer the scorer uses. Returns strings only (never None) so the join is unambiguous."""
    rec = _rec_dict(record)
    item_type = _candidate_item_type(rec)
    title = normalize_title(_candidate_title(rec))
    year = _candidate_year(rec)
    first_author = _candidate_first_author(rec)
    doi = _candidate_doi(rec) or ""
    return (item_type, title, year, first_author, doi)


def canonical_identity_string(record: Any) -> str:
    """The fixed-order, fixed-delimiter canonical message the HMAC is computed over. NEVER a
    dict/JSON serialization (key order is not guaranteed) — always this exact 5-field join."""
    return _IDENTITY_DELIM.join(normalized_identity(record))


def identity_sha256(record: Any) -> str:
    """sha256 hex of the canonical identity string — a compact, non-reversible key the
    ``validate_record`` PROV record and the validation-gate hook both use to find "an accept decision
    on file for THIS record" without embedding the full record (or the HMAC secret) in PROV."""
    return hashlib.sha256(canonical_identity_string(record).encode("utf-8")).hexdigest()


def compute_approval_token(record: Any, key: bytes) -> str:
    """HMAC-SHA256(key, canonical_identity_string(record)) as a hex digest."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    msg = canonical_identity_string(record).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_approval_token(record: Any, token: Optional[str], key: Optional[bytes]) -> bool:
    """Constant-time verification. False on ANY ambiguity (no key, no/empty token, mismatch) —
    fail-closed, never raises."""
    if not token or not key:
        return False
    try:
        expected = compute_approval_token(record, key)
    except Exception:
        return False
    return hmac.compare_digest(expected, str(token))
