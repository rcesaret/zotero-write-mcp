"""Routine Supervised v1.0 — the separate reviewed metadata update (PRD META-001/002/003).

Metadata enrichment is NEVER part of the destructive merge transaction (P-02). When the owner wants
a field improved, it happens here: a previewed, owner-approved, live-version-checked PATCH against
an explicit bibliographic allowlist. Unknown, identity, structural, and state fields are rejected
loudly BEFORE any mutation (META-002); a live version change between preview and apply blocks
without touching the item (META-003); the applied update is verified by re-read and recorded with
before/after images.
"""
from __future__ import annotations

from typing import Any, Optional

from zotero_write_mcp import __version__
from zotero_write_mcp.gateway import ConcurrencyConflictError
from zotero_write_mcp.merge import _citekey_from_extra, _unwrap
from zotero_write_mcp.merge_live import live_merge_enabled, unresolved_transactions
from zotero_write_mcp.provenance import ProvenanceStore, canonical_json, sha256_hex

META_PROPOSED = "meta_update_proposed"
META_APPLIED = "meta_update_applied"
META_BLOCKED = "meta_update_blocked"

# The EXPLICIT bibliographic allowlist (META-002). Deliberately conservative: ordinary scalar
# bibliographic fields plus the structured creators array. It matches the field classes actually
# reconciled during the S2 production run (all ordinary bibliographic scalars). NOT included, by
# design:
#   * identity:   citationKey, and `extra` (carries the pinned BBT `Citation Key:` + tex.ids lines);
#   * structure:  collections, tags, relations, parentItem, itemType;
#   * state:      deleted, version, dateAdded, dateModified, key, mtime;
#   * anything not listed (unknown fields are rejected, not passed through).
# Extending the list is a reviewed contract change, not a runtime option.
METADATA_ALLOWLIST = frozenset({
    "title", "shortTitle", "abstractNote", "date", "language", "rights",
    "publicationTitle", "journalAbbreviation", "volume", "issue", "pages", "series",
    "seriesTitle", "seriesText", "seriesNumber", "edition", "place", "publisher",
    "numPages", "numberOfVolumes", "bookTitle", "proceedingsTitle", "conferenceName",
    "university", "institution", "thesisType", "reportType", "reportNumber",
    "DOI", "ISBN", "ISSN", "url", "accessDate", "archive", "archiveLocation",
    "libraryCatalog", "callNumber", "section", "creators",
})

_REJECT_REASONS = {
    "citationKey": "identity (the pinned citation key is the item's identity)",
    "extra": "identity (extra carries the pinned BBT 'Citation Key:' and tex.ids alias lines)",
    "collections": "structure", "tags": "structure", "relations": "structure",
    "parentItem": "structure", "itemType": "structure",
    "deleted": "state", "version": "state", "dateAdded": "state", "dateModified": "state",
    "key": "state", "mtime": "state",
}


def classify_fields(changes: dict) -> tuple:
    """Split a requested change set into (allowed, rejected) where rejected maps field -> reason.
    Everything not on the allowlist is rejected — unknown fields included (META-002)."""
    allowed: dict = {}
    rejected: dict = {}
    for fld, val in (changes or {}).items():
        if fld in METADATA_ALLOWLIST:
            allowed[fld] = val
        else:
            rejected[fld] = _REJECT_REASONS.get(fld, "unknown field (not on the explicit allowlist)")
    return allowed, rejected


def _proposal_id(item_key: str, version: int, changes: dict) -> str:
    return "META-" + sha256_hex(canonical_json(
        {"item": item_key, "version": int(version), "changes": changes}))[:16]


def propose_metadata_update(reader: Any, prov: ProvenanceStore, item_key: str, changes: dict) -> dict:
    """Read-only preview of ONE metadata update. Rejects inadmissible fields before anything else;
    captures the live version; records an immutable proposal whose id is content-bound to
    (item, version, changes) — any live change to the item voids the approval."""
    allowed, rejected = classify_fields(changes)
    if rejected:
        return {"error": "inadmissible fields rejected before any mutation (META-002)",
                "rejected": rejected, "proposal_id": None}
    if not allowed:
        return {"error": "no fields to change", "rejected": {}, "proposal_id": None}
    raw = reader.get_item(item_key)
    _, version, data = _unwrap(raw)
    pid = _proposal_id(item_key, version, allowed)
    preview = {fld: {"from": data.get(fld), "to": val} for fld, val in allowed.items()}
    unchanged = [fld for fld, d in preview.items() if d["from"] == d["to"]]
    prov.record(activity=META_PROPOSED, item_key=item_key, agent="meta-update",
                tool_version=__version__,
                params={"proposal_id": pid, "item_key": item_key, "item_version": version,
                        "changes": allowed})
    return {
        "proposal_id": pid, "item_key": item_key, "item_version": version,
        "changes": preview, "no_op_fields": unchanged,
        "citekey": _citekey_from_extra(data.get("extra")) or data.get("citationKey"),
        "warning": ("PREVIEW ONLY — apply_metadata_update re-checks the live version; approval of "
                    "this proposal_id is void if the item changes (a changed version changes the "
                    "outcome to blocked)."),
    }


def apply_metadata_update(proposal_id: str, reader: Any, gateway: Any, prov: ProvenanceStore, *,
                          library_id: int, library_type: str = "user") -> dict:
    """Apply ONE owner-approved metadata proposal with an engine-owned live version check.

    Outcomes (explicit, REC-002-style): ``applied`` | ``blocked_before_write`` |
    ``already_applied`` | ``shadow``. A version mismatch or 412 blocks WITHOUT overwriting the live
    item (META-003). Shadow unless the supervised live window is open (same out-of-band token as
    the merge transaction)."""

    def blocked(reason: str) -> dict:
        prov.record(activity=META_BLOCKED, agent="meta-update", tool_version=__version__,
                    params={"proposal_id": proposal_id, "reason": reason})
        return {"proposal_id": proposal_id, "state": "blocked_before_write", "reason": reason,
                "live_write_performed": False}

    integrity = prov.scan_integrity()
    if integrity["status"] == "blocked":
        return blocked("PROV log integrity is blocked (LOG-001); resolve damage first.")
    if unresolved_transactions(prov):
        return blocked("unresolved transaction(s) exist; destructive/mutating work is refused "
                       "until resolved (REC-006).")

    proposal = None
    for r in prov.iter_records():
        p = r.get("params") or {}
        if p.get("proposal_id") != proposal_id:
            continue
        if r.get("activity") == META_APPLIED:
            return {"proposal_id": proposal_id, "state": "already_applied",
                    "reason": "this proposal was already applied; no write repeated (IDEM-001)",
                    "live_write_performed": False}
        if r.get("activity") == META_PROPOSED:
            proposal = p
    if proposal is None:
        return blocked(f"unknown proposal_id {proposal_id!r} — call propose_metadata_update first.")

    item_key = proposal["item_key"]
    changes = dict(proposal["changes"])
    allowed, rejected = classify_fields(changes)    # defense in depth: re-validate at apply time
    if rejected or set(allowed) != set(changes):
        return blocked(f"proposal contains inadmissible fields {sorted(rejected)}; refused.")

    raw = reader.get_item(item_key)
    _, live_version, live_data = _unwrap(raw)
    if live_version != int(proposal["item_version"]):
        return blocked(f"live version {live_version} != proposed {proposal['item_version']} — the "
                       "item changed since preview; the live value was NOT overwritten (META-003). "
                       "Re-propose against the current state.")

    if not live_merge_enabled():
        return {"proposal_id": proposal_id, "state": "shadow",
                "reason": "supervised live window not open (out-of-band token absent) — no mutation",
                "live_write_performed": False,
                "would_change": {f: {"from": live_data.get(f), "to": v} for f, v in allowed.items()}}

    before = dict(live_data)
    try:
        gateway.update_item(library_id, item_key, allowed, live_version,
                            library_type=library_type, retry_on_412=False)
    except ConcurrencyConflictError:
        return blocked("concurrent edit detected at PATCH time (412); the live value was NOT "
                       "overwritten (META-003). Re-propose against the current state.")

    _, after_version, after_data = _unwrap(reader.get_item(item_key))
    mismatch = [f for f, v in allowed.items() if after_data.get(f) != v]
    prov.record(activity=META_APPLIED, item_key=item_key, before=before, after=dict(after_data),
                agent="meta-update", tool_version=__version__,
                params={"proposal_id": proposal_id, "changes": allowed,
                        "prior_version": live_version, "new_version": after_version,
                        "terminal_mismatch": mismatch})
    return {"proposal_id": proposal_id,
            "state": "applied" if not mismatch else "applied_with_mismatch",
            "reason": ("metadata update applied and verified by re-read" if not mismatch else
                       f"applied but re-read disagrees on {mismatch} — inspect the item"),
            "live_write_performed": True,
            "prior_version": live_version, "new_version": after_version,
            "changes": {f: {"from": before.get(f), "to": after_data.get(f)} for f in allowed}}
