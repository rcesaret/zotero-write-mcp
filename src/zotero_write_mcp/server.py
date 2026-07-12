"""Zotero Write MCP Server — creation, editing, and merging tools."""
import json
import os
from typing import Any, Optional

from fastmcp import FastMCP

from zotero_write_mcp.client import ZoteroClient
from zotero_write_mcp.safety import RiskLevel, requires_confirmation
from zotero_write_mcp.utils import (
    resolve_doi,
    parse_bibtex,
    compute_duplicate_score,
    format_item_summary,
    format_item_detail,
    title_similarity,
)
# Phase-2 merge transaction engine (the gated tools wired below; aliased to avoid colliding with the
# @mcp.tool() function names).
from zotero_write_mcp.merge import (
    snapshot_cluster as _eng_snapshot, build_cluster as _eng_build, rollback_merge as _eng_rollback,
    shadow_merge as _eng_shadow_merge,
)
from zotero_write_mcp.merge_live import (
    merge_cluster as _eng_merge, commit_merge as _eng_commit, load_snapshot as _eng_load_snapshot,
    reconcile_orphan_commits as _eng_reconcile, WebClusterReader, library_item_base,
)
from zotero_write_mcp.dedup import dedup_scan as _eng_dedup
from zotero_write_mcp.observability import (
    query_provenance as _eng_query_prov, daily_report as _eng_daily,
    prov_coverage_report as _eng_prov_coverage,
)
# S5a (Phase-6 tooling): read-only observability + tooling — a library-wide citekey scanner and the
# web-API pager it (and the dashboard) share. NEVER a Zotero mutation path.
from zotero_write_mcp.webscan import web_items as _eng_web_items
from zotero_write_mcp import citekeys as _eng_citekeys
# Phase-3 validation (sprint S3): read-only source clients + the pure scorer/gate. validate_record
# below makes ZERO Zotero writes (INV-COMP) -- it only reads external authorities + logs a read-only
# PROV "informed-by" record.
from zotero_write_mcp import sources as _eng_sources
from zotero_write_mcp import validation as _eng_validation
# Phase-4 ingest (sprint S4): the deterministic routed PDF->metadata extractor (TC-8).
# extract_pdf_metadata below makes ZERO Zotero writes (INV-COMP) -- it only reads local files +
# external authorities and logs a read-only PROV "informed-by" record.
from zotero_write_mcp import ingest as _eng_ingest
from zotero_write_mcp import __version__ as _ENGINE_VERSION

mcp = FastMCP(
    "ZoteroWrite",
    instructions=(
        "Zotero library write operations: create, edit, and merge entries. "
        "Use these tools to add new references, update metadata, find and merge "
        "duplicates, and audit library entries. The companion 'zotero-mcp' server "
        "provides read/search capabilities."
    ),
)

# Lazy-initialized client
_client: Optional[ZoteroClient] = None

# Summary of the most-recent startup crash-recovery (F4 reconcile) pass; exposed via reconcile_orphans.
_startup_reconcile: Optional[dict] = None


def get_client() -> ZoteroClient:
    global _client
    if _client is None:
        _client = ZoteroClient()
        _run_startup_reconcile(_client)   # F4 crash-recovery before the merge chain resumes (REV5 finding 3)
    return _client


def _run_startup_reconcile(zot: ZoteroClient) -> dict:
    """Roll back any orphaned ``commit_merge_intent`` (a live merge that crashed mid-trash — intent
    logged, no result) from its snapshot, at client init, BEFORE the merge chain resumes. Read-safe:
    ``reconcile_orphan_commits`` performs only the sanctioned rollback (un-trash + revert + reparent,
    never purge/recreate on the trash path) and is a no-op when there are no orphans — the common case
    (orphans exist only after a real live-merge crash). An orphan whose snapshot blob is MISSING is
    surfaced LOUDLY (non-recoverable — needs human recovery), never a silent skip. A failure here never
    bricks client init."""
    global _startup_reconcile
    summary = {"orphans_found": 0, "reconciled": 0, "rollback_failed": 0,
               "no_snapshot_blob": [], "error": None}
    try:
        reader = WebClusterReader(zot, zot.library_id)
        outcomes = _eng_reconcile(zot.prov, reader, zot.gateway, library_id=zot.library_id)
        summary["orphans_found"] = len(outcomes)
        summary["reconciled"] = sum(1 for o in outcomes if o.get("status") == "reconciled")
        summary["rollback_failed"] = sum(1 for o in outcomes if o.get("status") == "rollback_failed")
        summary["no_snapshot_blob"] = [o.get("snapshot_id") for o in outcomes
                                       if o.get("status") == "no-snapshot-blob"]
    except Exception as e:                       # never let recovery brick the server
        summary["error"] = f"{type(e).__name__}: {e}"
    if summary["no_snapshot_blob"] or summary["rollback_failed"] or summary["error"]:
        # Loud, config-independent surfacing of a non-recoverable / failed orphan.
        import sys as _sys
        _sys.stderr.write(
            "[startup-reconcile] ATTENTION — orphaned merge(s) need human review: "
            f"no_snapshot_blob={summary['no_snapshot_blob']} "
            f"rollback_failed={summary['rollback_failed']} error={summary['error']}\n")
    _startup_reconcile = summary
    return summary


# ═══════════════════════════════════════════════════════════════════
#  CREATION TOOLS
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
def create_item(
    item_type: str,
    title: str,
    creators: list[dict],
    date: str = "",
    publication_title: str = "",
    volume: str = "",
    issue: str = "",
    pages: str = "",
    doi: str = "",
    publisher: str = "",
    place: str = "",
    url: str = "",
    abstract: str = "",
    isbn: str = "",
    issn: str = "",
    book_title: str = "",
    edition: str = "",
    series: str = "",
    extra_fields: Optional[dict] = None,
    tags: Optional[list[str]] = None,
    check_duplicates: bool = True,
    approval_token: str = "",
) -> str:
    """Create a new Zotero item from explicit metadata fields.

    Args:
        item_type: Zotero item type (journalArticle, book, bookSection, conferencePaper, thesis, report, etc.)
        title: Item title
        creators: List of creator dicts, each with 'creatorType', 'firstName', 'lastName'
                  Example: [{"creatorType": "author", "firstName": "John", "lastName": "Smith"}]
        date: Publication date (e.g. "2024", "2024-03-15")
        publication_title: Journal or publication name
        volume: Volume number
        issue: Issue number
        pages: Page range (e.g. "1-25")
        doi: Digital Object Identifier
        publisher: Publisher name
        place: Place of publication
        url: URL
        abstract: Abstract text
        isbn: ISBN
        issn: ISSN
        book_title: Book title (for bookSection items)
        edition: Edition
        series: Series name
        extra_fields: Additional Zotero fields as key-value pairs
        tags: List of tag strings to apply
        check_duplicates: If True, check for duplicates before creating (default True)
        approval_token: Out-of-band HMAC approval token (sprint S3, `scripts/approve_record.py`) for
            the validation-gate PreToolUse hook. This function does NOT itself validate the token —
            the hook re-derives and checks it BEFORE this call ever executes; it is accepted here
            purely so it is visible in the tool call for the hook to inspect. Omit it when an accept
            decision from `validate_record` is already on file for this exact record.
    """
    zot = get_client()

    # Build item data
    item_data = {"itemType": item_type, "title": title, "creators": creators}
    field_map = {
        "date": date, "publicationTitle": publication_title,
        "volume": volume, "issue": issue, "pages": pages,
        "DOI": doi, "publisher": publisher, "place": place,
        "url": url, "abstractNote": abstract, "ISBN": isbn,
        "ISSN": issn, "bookTitle": book_title, "edition": edition,
        "series": series,
    }
    for k, v in field_map.items():
        if v:
            item_data[k] = v
    if extra_fields:
        item_data.update(extra_fields)
    if tags:
        item_data["tags"] = [{"tag": t} for t in tags]

    # Duplicate check
    if check_duplicates and title:
        existing = zot.search_items(title, limit=5)
        dupes = []
        for ex in existing:
            score = compute_duplicate_score({"data": item_data}, ex)
            if score > 0.6:
                dupes.append((score, ex))
        if dupes:
            dupes.sort(key=lambda x: -x[0])
            lines = ["⚠️ **Potential duplicates found:**\n"]
            for score, ex in dupes:
                lines.append(f"- Score {score:.0%}: {format_item_summary(ex)}")
            lines.append(f"\nTo create anyway, call again with `check_duplicates=False`.")
            lines.append(f"To update an existing entry instead, use `update_item_fields`.")
            return "\n".join(lines)

    result = zot.create_items([item_data])
    success = result.get("success", {})
    failed = result.get("failed", {})

    if success:
        new_key = list(success.values())[0] if isinstance(list(success.values())[0], str) else success
        return f"✅ Item created successfully. Key: {json.dumps(success)}"
    elif failed:
        return f"❌ Creation failed: {json.dumps(failed)}"
    else:
        return f"Response: {json.dumps(result)}"


@mcp.tool()
def create_item_from_doi(
    doi: str,
    tags: Optional[list[str]] = None,
    check_duplicates: bool = True,
) -> str:
    """Create a new Zotero item by looking up metadata from a DOI via Crossref.

    Args:
        doi: The DOI to look up (e.g. "10.1234/example.5678")
        tags: Optional list of tag strings
        check_duplicates: Check for existing entries before creating
    """
    zot = get_client()

    meta = resolve_doi(doi)
    if meta is None:
        return f"❌ Could not resolve DOI: {doi}. Check that it is valid."

    if tags:
        meta["tags"] = [{"tag": t} for t in tags]

    # Duplicate check
    if check_duplicates:
        # Check by DOI first
        existing = zot.search_items(doi, limit=5, qmode="everything")
        for ex in existing:
            ex_doi = ex.get("data", {}).get("DOI", "").strip().lower()
            if ex_doi and ex_doi == doi.strip().lower():
                return (
                    f"⚠️ **Item with this DOI already exists:**\n"
                    f"{format_item_summary(ex)}\n\n"
                    f"To create a duplicate anyway, call with `check_duplicates=False`."
                )
        # Check by title
        title = meta.get("title", "")
        if title:
            existing = zot.search_items(title, limit=5)
            dupes = []
            for ex in existing:
                score = compute_duplicate_score({"data": meta}, ex)
                if score > 0.6:
                    dupes.append((score, ex))
            if dupes:
                dupes.sort(key=lambda x: -x[0])
                lines = [f"**Resolved DOI metadata:**", f"Title: {title}",
                         f"Authors: {', '.join(c.get('lastName','') for c in meta.get('creators',[]))}",
                         "", "⚠️ **Potential duplicates found:**"]
                for score, ex in dupes:
                    lines.append(f"- Score {score:.0%}: {format_item_summary(ex)}")
                lines.append(f"\nCall with `check_duplicates=False` to create anyway.")
                return "\n".join(lines)

    result = zot.create_items([meta])
    success = result.get("success", {})
    failed = result.get("failed", {})

    if success:
        return (
            f"✅ Item created from DOI.\n"
            f"**Key:** {json.dumps(success)}\n"
            f"**Title:** {meta.get('title', '')}\n"
            f"**Type:** {meta.get('itemType', '')}"
        )
    elif failed:
        return f"❌ Creation failed: {json.dumps(failed)}"
    else:
        return f"Response: {json.dumps(result)}"


@mcp.tool()
def create_item_from_bibtex(
    bibtex: str,
    tags: Optional[list[str]] = None,
    check_duplicates: bool = True,
) -> str:
    """Create a new Zotero item by parsing a BibTeX entry.

    Args:
        bibtex: A BibTeX entry string (e.g. '@article{key, title={...}, ...}')
        tags: Optional list of tag strings
        check_duplicates: Check for existing entries before creating
    """
    zot = get_client()

    meta = parse_bibtex(bibtex)
    if meta is None:
        return "❌ Could not parse BibTeX string. Check formatting."

    if tags:
        meta["tags"] = [{"tag": t} for t in tags]

    if check_duplicates and meta.get("title"):
        existing = zot.search_items(meta["title"], limit=5)
        dupes = []
        for ex in existing:
            score = compute_duplicate_score({"data": meta}, ex)
            if score > 0.6:
                dupes.append((score, ex))
        if dupes:
            dupes.sort(key=lambda x: -x[0])
            lines = ["⚠️ **Potential duplicates found:**"]
            for score, ex in dupes:
                lines.append(f"- Score {score:.0%}: {format_item_summary(ex)}")
            lines.append(f"\nParsed title: {meta.get('title','')}")
            lines.append(f"Call with `check_duplicates=False` to create anyway.")
            return "\n".join(lines)

    result = zot.create_items([meta])
    success = result.get("success", {})

    if success:
        return (
            f"✅ Item created from BibTeX.\n"
            f"**Key:** {json.dumps(success)}\n"
            f"**Title:** {meta.get('title', '')}"
        )
    else:
        return f"Response: {json.dumps(result)}"


# ═══════════════════════════════════════════════════════════════════
#  EDITING TOOLS
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
def update_item_fields(
    item_key: str,
    fields: dict,
) -> str:
    """Update specific fields on an existing Zotero item.

    Args:
        item_key: The Zotero item key (e.g. "BCBLBCMI")
        fields: Dictionary of field names and new values.
                Supported fields include: title, date, publicationTitle, volume,
                issue, pages, DOI, publisher, place, url, abstractNote, ISBN,
                ISSN, bookTitle, edition, series, extra, rights, language, etc.
                For creators, pass a full creators array.
                For tags, pass a list of {"tag": "name"} dicts.
    """
    zot = get_client()
    item = zot.get_item_web(item_key)  # web read for current version
    version = item.get("version", item.get("data", {}).get("version"))
    data = item.get("data", item)

    # Update fields
    for k, v in fields.items():
        data[k] = v

    result = zot.update_item(item_key, data, version)
    return f"✅ Updated item {item_key}. Fields changed: {', '.join(fields.keys())}"


@mcp.tool()
def add_tags_to_item(
    item_key: str,
    tags: list[str],
) -> str:
    """Add tags to an existing Zotero item without removing existing tags.

    Args:
        item_key: The Zotero item key
        tags: List of tag strings to add
    """
    zot = get_client()
    item = zot.get_item_web(item_key)  # web read for current version
    version = item.get("version", item.get("data", {}).get("version"))
    data = item.get("data", item)

    existing_tags = {t["tag"] for t in data.get("tags", [])}
    new_tags = [{"tag": t} for t in tags if t not in existing_tags]
    data["tags"] = data.get("tags", []) + new_tags

    if not new_tags:
        return f"ℹ️ All tags already present on {item_key}."

    zot.update_item(item_key, data, version)
    added = [t["tag"] for t in new_tags]
    return f"✅ Added {len(added)} tag(s) to {item_key}: {', '.join(added)}"


@mcp.tool()
def remove_tags_from_item(
    item_key: str,
    tags: list[str],
) -> str:
    """Remove specific tags from a Zotero item.

    Args:
        item_key: The Zotero item key
        tags: List of tag strings to remove
    """
    zot = get_client()
    item = zot.get_item_web(item_key)  # web read for current version
    version = item.get("version", item.get("data", {}).get("version"))
    data = item.get("data", item)

    tags_set = set(tags)
    original_count = len(data.get("tags", []))
    data["tags"] = [t for t in data.get("tags", []) if t["tag"] not in tags_set]
    removed_count = original_count - len(data["tags"])

    if removed_count == 0:
        return f"ℹ️ None of the specified tags found on {item_key}."

    zot.update_item(item_key, data, version)
    return f"✅ Removed {removed_count} tag(s) from {item_key}."


@mcp.tool()
def validate_item(
    item_key: str,
) -> str:
    """Check a Zotero item for completeness issues (missing fields, formatting problems).

    Args:
        item_key: The Zotero item key to validate
    """
    zot = get_client()
    item = zot.get_item(item_key)
    data = item.get("data", item)
    itype = data.get("itemType", "")

    issues = []
    warnings = []

    # Universal checks
    if not data.get("title"):
        issues.append("Missing title")
    if not data.get("creators"):
        issues.append("No creators/authors")
    if not data.get("date"):
        issues.append("Missing date")

    # Type-specific checks
    if itype == "journalArticle":
        if not data.get("publicationTitle"):
            issues.append("Missing journal name (publicationTitle)")
        if not data.get("volume"):
            warnings.append("Missing volume")
        if not data.get("pages"):
            warnings.append("Missing pages")
        if not data.get("DOI"):
            warnings.append("Missing DOI")
    elif itype == "book":
        if not data.get("publisher"):
            issues.append("Missing publisher")
        if not data.get("ISBN"):
            warnings.append("Missing ISBN")
    elif itype == "bookSection":
        if not data.get("bookTitle"):
            issues.append("Missing book title")
        if not data.get("publisher"):
            warnings.append("Missing publisher")
        if not data.get("pages"):
            warnings.append("Missing pages")

    # Formatting checks
    if data.get("title", "").isupper():
        warnings.append("Title is ALL CAPS — may need title case")
    if data.get("pages") and "--" in data.get("pages", ""):
        warnings.append("Pages use LaTeX-style '--' instead of '-'")

    # Creator checks
    for i, c in enumerate(data.get("creators", [])):
        if not c.get("lastName"):
            issues.append(f"Creator {i}: missing lastName")
        if not c.get("firstName") and c.get("creatorType") == "author":
            warnings.append(f"Creator {i} ({c.get('lastName','')}): missing firstName")

    header = format_item_summary(item)
    if not issues and not warnings:
        return f"✅ **{header}**\nNo issues found."

    lines = [f"**{header}**\n"]
    if issues:
        lines.append(f"**Issues ({len(issues)}):**")
        for iss in issues:
            lines.append(f"  ❌ {iss}")
    if warnings:
        lines.append(f"**Warnings ({len(warnings)}):**")
        for w in warnings:
            lines.append(f"  ⚠️ {w}")

    return "\n".join(lines)


@mcp.tool()
def validate_record(
    item_type: str,
    title: str,
    creators: list[dict],
    date: str = "",
    doi: str = "",
    publication_title: str = "",
    book_title: str = "",
) -> str:
    """Phase-3 validation (sprint S3) — READ-ONLY. Gathers agreement from external bibliographic
    authorities (DOI content-negotiation, Crossref, OpenAlex if keyed, Semantic Scholar, DataCite) for
    a CANDIDATE record and returns a calibrated `{p, decision, evidence, conflicts}` verdict via the
    PINNED 3-way gate (accept/flag/reject). Makes ZERO Zotero writes and zero library mutations of any
    kind (INV-COMP, ADR-005) — it only reads external authorities + logs a read-only PROV
    "informed-by" record. Confidence is cross-source agreement, NEVER an LLM self-report: nothing here
    reads a caller-supplied confidence/probability field.

    Degrades cleanly when a source is unavailable (e.g. no OPENALEX_API_KEY): composes over whatever
    authorities ARE available, surfaces an `"<authority>: unavailable ..."` evidence note, and never
    auto-accepts on fewer than the required authorities (PLAN1 SS4).

    Args:
        item_type: Zotero item type of the CANDIDATE record being validated (journalArticle, book, ...)
        title: Candidate title
        creators: List of creator dicts, each with 'creatorType', 'firstName', 'lastName'
        date: Candidate publication date/year
        doi: Candidate DOI, if known (drives a direct doi-lookup path over search)
        publication_title: Candidate journal/venue name (for journalArticle-shaped records)
        book_title: Candidate book title (for bookSection-shaped records; used as the venue field)
    """
    candidate = {
        "itemType": item_type, "title": title, "creators": creators or [], "date": date,
        "DOI": doi, "publicationTitle": publication_title, "bookTitle": book_title,
    }

    authorities = _eng_sources.default_authorities()
    doi_lookup_attempted = bool(doi)
    if doi:
        gathered = _eng_sources.gather_by_doi(doi, authorities)
    else:
        gathered = _eng_sources.gather_by_search(candidate, authorities)

    calibration = _eng_validation.load_calibration()
    verdict = _eng_validation.build_validation_result(
        candidate, gathered.records, calibration,
        doi_lookup_attempted=doi_lookup_attempted, extra_evidence=gathered.evidence,
    )
    verdict["available_authorities"] = gathered.available
    verdict["answered_authorities"] = gathered.answered

    # Read-only PROV "informed-by" record: a validation CONSULTED authorities; it is NOT a mutation.
    # before/after are intentionally omitted (both json_sha256 stay null) so this record is
    # unambiguously distinct from every create/update/merge PROV entry in the same log.
    zot = get_client()
    zot.prov.record(
        activity="validate_record",
        agent="validation-engine", tool_version=_ENGINE_VERSION,
        params={
            "identity_sha256": _eng_validation.identity_sha256(candidate),
            "decision": verdict["decision"],
            "p": verdict["p"],
            "available_authorities": gathered.available,
            "answered_authorities": gathered.answered,
        },
        source="; ".join(gathered.evidence) or None,
        confidence=verdict["p"],
    )
    return json.dumps(verdict)


@mcp.tool()
def extract_pdf_metadata(
    pdf_path: str,
    md_path: str = "",
    content_list_path: str = "",
    mineru_report_path: str = "",
    lang_hint: str = "",
    route_hint: str = "",
) -> str:
    """Phase-4 ingest (sprint S4) — READ-ONLY. Extracts structured bibliographic metadata for a
    PDF via the deterministic six-stage TC-8 pipeline (triage → route → structured parse →
    authority match → cross-source agreement score → compose) and returns `{fields,
    per_field_source, agreement_confidence, needs_review, needs_review_reasons, decision,
    conflicts, evidence, route, validation, identifiers}`. Makes ZERO Zotero writes and zero
    library mutations of any kind — it only reads local files (the PDF/md/MinerU artifacts) +
    external authorities and logs a read-only PROV "informed-by" record. Confidence is
    cross-source agreement, NEVER an LLM self-report: nothing here reads a caller-supplied
    confidence/probability field (INV-COMP; any such key on a candidate is stripped before
    scoring).

    Degrades cleanly: when GROBID is not running (Path A unavailable) the parse falls back to the
    mineru-markdown-fixer seed and the result is ALWAYS `needs_review=true`
    ("path_b_never_auto_create" / "grobid_unavailable") — a degraded or Path-B parse never
    auto-creates, no matter how well authorities agree (PLAN2 §5).

    Args:
        pdf_path: Path to the source PDF
        md_path: Path to the mineru-markdown-fixer corrected .md (carries the YAML/BibTeX seed)
        content_list_path: Path to MinerU's content_list.json (block-mix routing signal)
        mineru_report_path: Path to the MinerU --json run-report (settings.is_ocr routing signal)
        lang_hint: Language hint ("en", ...); wins over detection
        route_hint: "stem" | "humanities" | "" — an owner/skill routing hint
    """
    result = _eng_ingest.extract_pdf_metadata(
        pdf_path,
        md_path=md_path,
        content_list_path=content_list_path,
        mineru_report_path=mineru_report_path,
        lang_hint=lang_hint,
        route_hint=route_hint,
        grobid=_eng_ingest.GrobidClient(),
    )

    # Read-only PROV "informed-by" record mirroring validate_record's: an extraction CONSULTED
    # local files + authorities; it is NOT a mutation. before/after are intentionally omitted
    # (both json_sha256 stay null) so this record is unambiguously distinct from every
    # create/update/merge PROV entry in the same log.
    zot = get_client()
    zot.prov.record(
        activity="extract_pdf_metadata",
        agent="ingest-engine", tool_version=_ENGINE_VERSION,
        params={
            "identity_sha256": _eng_validation.identity_sha256(result["fields"]),
            "decision": result["decision"],
            "p": result["agreement_confidence"],
            "needs_review": result["needs_review"],
            "route": result["route"]["decision"],
            "parse_path": result["route"]["parse_path"],
        },
        source="; ".join(result["evidence"]) or None,
        confidence=result["agreement_confidence"],
    )
    return json.dumps(result)


@mcp.tool()
def get_item_type_fields(
    item_type: str,
) -> str:
    """Get the list of valid fields for a Zotero item type.

    Args:
        item_type: Zotero item type (journalArticle, book, bookSection, etc.)
    """
    zot = get_client()
    template = zot.get_item_template(item_type)
    lines = [f"**Fields for `{item_type}`:**\n"]
    for key, val in sorted(template.items()):
        if key == "creators":
            lines.append(f"  - **creators**: list of {{creatorType, firstName, lastName}}")
        elif key == "tags":
            lines.append(f"  - **tags**: list of {{tag: string}}")
        else:
            lines.append(f"  - **{key}**: {type(val).__name__} (default: {repr(val) if val else 'empty'})")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  MERGING / DUPLICATE TOOLS
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
def find_duplicate_candidates(
    min_score: float = 0.65,
    limit: int = 100,
    start: int = 0,
    batch_size: int = 50,
) -> str:
    """Scan the Zotero library for potential duplicate items using fuzzy matching.

    ADVISORY ONLY (REV3 F4): these fuzzy scores feed the human-review queue; the deterministic
    `dedup_scan` (exact DOI OR exact normalized title+year+first-author) is the SOLE auto-accept /
    merge path. Never merge on this tool's output.

    Compares items by title, authors, date, and DOI. Returns pairs above the
    minimum similarity score. This can take time for large libraries.

    Args:
        min_score: Minimum similarity score (0-1) to report as duplicate (default 0.65)
        limit: Maximum number of items to scan from the library
        start: Starting offset for pagination through the library
        batch_size: Items to fetch per API call (max 100)
    """
    zot = get_client()

    # Fetch items
    items = zot.get_all_items(limit=min(batch_size, limit), start=start)
    if len(items) < limit and len(items) == batch_size:
        # Fetch more
        while len(items) < limit:
            more = zot.get_all_items(limit=min(batch_size, limit - len(items)),
                                     start=start + len(items))
            if not more:
                break
            items.extend(more)

    if len(items) < 2:
        return "Library has fewer than 2 items to compare."

    # Pairwise comparison
    duplicates = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            score = compute_duplicate_score(items[i], items[j])
            if score >= min_score:
                duplicates.append((score, items[i], items[j]))

    duplicates.sort(key=lambda x: -x[0])

    if not duplicates:
        return f"✅ No duplicates found above {min_score:.0%} threshold in {len(items)} items scanned."

    lines = [f"**Found {len(duplicates)} potential duplicate pair(s)** (scanned {len(items)} items):\n"]
    for idx, (score, a, b) in enumerate(duplicates[:20], 1):
        lines.append(f"### Pair {idx} — Similarity: {score:.0%}")
        lines.append(f"  A: {format_item_summary(a)}")
        lines.append(f"  B: {format_item_summary(b)}")
        lines.append("")

    if len(duplicates) > 20:
        lines.append(f"... and {len(duplicates) - 20} more pairs.")

    lines.append("\nUse `compare_items_for_merge` to see detailed field-by-field comparison.")
    return "\n".join(lines)


@mcp.tool()
def find_duplicates_for_item(
    item_key: str,
    min_score: float = 0.5,
    search_limit: int = 20,
) -> str:
    """Find potential duplicates of a specific item in the library.

    ADVISORY ONLY (REV3 F4): fuzzy scores for the human-review queue; the deterministic `dedup_scan` is
    the SOLE auto-accept / merge path.

    Args:
        item_key: The item key to find duplicates of
        min_score: Minimum similarity score to report
        search_limit: How many search results to check
    """
    zot = get_client()
    target = zot.get_item(item_key)
    target_data = target.get("data", target)
    title = target_data.get("title", "")

    if not title:
        return f"❌ Item {item_key} has no title to search for duplicates."

    # Search by title words
    words = title.split()[:5]
    query = " ".join(words)
    candidates = zot.search_items(query, limit=search_limit)

    dupes = []
    for cand in candidates:
        cand_key = cand.get("key", cand.get("data", {}).get("key", ""))
        if cand_key == item_key:
            continue
        score = compute_duplicate_score(target, cand)
        if score >= min_score:
            dupes.append((score, cand))

    dupes.sort(key=lambda x: -x[0])

    if not dupes:
        return f"✅ No duplicates found for {format_item_summary(target)}"

    lines = [f"**Duplicates for:** {format_item_summary(target)}\n"]
    for score, d in dupes:
        lines.append(f"- {score:.0%}: {format_item_summary(d)}")

    lines.append("\nUse `compare_items_for_merge` for detailed field comparison.")
    return "\n".join(lines)


@mcp.tool()
def compare_items_for_merge(
    item_key_a: str,
    item_key_b: str,
) -> str:
    """Show a detailed side-by-side comparison of two items for merge decisions.

    ADVISORY ONLY (REV3 F4): a human-review aid; the deterministic `dedup_scan` + the verify-gated merge
    chain are the SOLE auto-accept / merge path.

    Displays all fields from both items, highlighting differences to help
    decide which fields to keep.

    Args:
        item_key_a: First item key
        item_key_b: Second item key
    """
    zot = get_client()
    item_a = zot.get_item(item_key_a)
    item_b = zot.get_item(item_key_b)
    data_a = item_a.get("data", item_a)
    data_b = item_b.get("data", item_b)

    score = compute_duplicate_score(item_a, item_b)

    lines = [
        f"## Merge Comparison (similarity: {score:.0%})\n",
        f"### Item A: {item_key_a}",
        format_item_detail(item_a),
        "",
        f"### Item B: {item_key_b}",
        format_item_detail(item_b),
        "",
        "### Field-by-Field Differences\n",
    ]

    # Compare all fields
    all_fields = set(list(data_a.keys()) + list(data_b.keys()))
    skip = {"key", "version", "dateAdded", "dateModified", "relations", "collections"}
    diff_count = 0

    for field in sorted(all_fields - skip):
        val_a = data_a.get(field, "")
        val_b = data_b.get(field, "")

        if field == "creators":
            str_a = json.dumps(val_a, ensure_ascii=False) if val_a else ""
            str_b = json.dumps(val_b, ensure_ascii=False) if val_b else ""
            if str_a != str_b:
                diff_count += 1
                lines.append(f"**{field}:** DIFFERS")
                lines.append(f"  A: {str_a[:200]}")
                lines.append(f"  B: {str_b[:200]}")
        elif field == "tags":
            tags_a = {t.get("tag", "") for t in (val_a or [])}
            tags_b = {t.get("tag", "") for t in (val_b or [])}
            if tags_a != tags_b:
                diff_count += 1
                lines.append(f"**{field}:** DIFFERS")
                lines.append(f"  A: {tags_a}")
                lines.append(f"  B: {tags_b}")
        else:
            if str(val_a) != str(val_b):
                diff_count += 1
                a_label = "✅" if val_a and not val_b else ("" if val_a else "⬜")
                b_label = "✅" if val_b and not val_a else ("" if val_b else "⬜")
                lines.append(f"**{field}:**")
                lines.append(f"  A {a_label}: {str(val_a)[:150]}")
                lines.append(f"  B {b_label}: {str(val_b)[:150]}")

    if diff_count == 0:
        lines.append("No field differences found — items are identical.")
    else:
        lines.append(f"\n**{diff_count} field(s) differ.**")

    lines.append(
        "\nTo merge, use the gated chain: snapshot_cluster -> merge_cluster -> commit_merge "
        "(rollback_merge on failure). The legacy merge_items is retired."
    )
    return "\n".join(lines)


@mcp.tool()
def merge_items(
    primary_key: str,
    secondary_key: str,
    field_overrides: Optional[dict] = None,
    use_secondary_fields: Optional[list[str]] = None,
    merge_tags: bool = True,
    confirm: bool = False,
) -> str:
    """Merge two Zotero items by updating the primary and deleting the secondary.

    This is a DESTRUCTIVE operation. By default it returns a preview.
    Set confirm=True to execute.

    The primary item is kept. Fields from the secondary can selectively replace
    primary fields via use_secondary_fields or field_overrides.

    Args:
        primary_key: Item key to keep (the "winner")
        secondary_key: Item key to merge in and delete (the "loser")
        field_overrides: Explicit field values to set on the merged item.
                         These take highest priority.
        use_secondary_fields: List of field names where the secondary item's
                              value should replace the primary's.
        merge_tags: If True, combine tags from both items (default True)
        confirm: Must be True to execute. False returns a preview.
    """
    # RETIRED (review OBS-6): merge_items performed an UNGATED destructive merge (update primary + PURGE-
    # delete secondary) with no verify gate, bypassing the Phase-2 safety chain. It now refuses + redirects
    # to the gated transaction. (Also blocked by the merge-safety enforcer hook; the code below is dead.)
    return (
        "merge_items is RETIRED — it bypassed the Phase-2 verify / enable-token / observability gate and "
        "PURGE-deleted the secondary. Use the gated transaction instead:\n"
        "  1) snapshot_cluster(master_key, dup_keys) -> snapshot_id\n"
        "  2) merge_cluster(master_key, dup_keys, snapshot_id)\n"
        "  3) commit_merge(master_key, snapshot_id)  -> verify-gated TRASH (not purge); SHADOW unless the "
        "out-of-band ZOT_MERGE_LIVE_ENABLED env token is set AND observability is fresh\n"
        "  rollback_merge(snapshot_id) on failure.\n"
        "See memory/phase2-build-spec.md / .claude/rules/merge-safety.md."
    )
    # (S0 C.5 / REV3 F7) The old ungated update-primary + PURGE-delete-secondary body below the return was
    # unreachable dead code that kept a destructive purge path one deleted `return` from resurrection and
    # contradicted trash-not-purge. Deleted. The retirement stub above is the whole function now.


# ═══════════════════════════════════════════════════════════════════
#  PHASE-2 MERGE TRANSACTION ENGINE (gated; the ONLY sanctioned merge path)
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
def snapshot_cluster(master_key: str, dup_keys: list) -> str:
    """Capture an immutable before-image of a duplicate cluster (master + dups). Returns a `snapshot_id`
    that merge_cluster / commit_merge / rollback_merge consume — the snapshot IS the rollback index."""
    zot = get_client()
    reader = WebClusterReader(zot, zot.library_id)
    snap = _eng_snapshot(reader, master_key, list(dup_keys), prov=zot.prov)
    return json.dumps({"snapshot_id": snap.snapshot_id, "master_key": master_key,
                       "secondaries": list(dup_keys), "n_notes": len(snap.notes),
                       "n_attachments": len(snap.attachments)})


@mcp.tool()
def merge_cluster(master_key: str, dup_keys: list, snapshot_id: str, smart_fill: bool = False,
                  field_sources: Optional[dict] = None) -> str:
    """PATCH phase of a merge: reparent children to the master + union collections/tags/relations +
    dc:replaces. NO delete — reversible via rollback_merge. Aborts with NO writes on version drift.

    `field_sources` ({field: source_member_key}) is the owner-approved Phase-B field-level enrichment,
    applied verbatim from the named member and enforced by verify check #3 (a wrong value fails the gate,
    not the library). The returned `master_version` is the POST-PATCH master version — pass it into
    commit_merge's `expected_master_version` to pin the master across the merge_cluster→commit window."""
    zot = get_client()
    snap = _eng_load_snapshot(zot.prov, snapshot_id)
    if snap is None:
        return json.dumps({"error": f"unknown snapshot_id {snapshot_id}; call snapshot_cluster first"})
    reader = WebClusterReader(zot, zot.library_id)
    plan = _eng_merge(snap, reader, zot.gateway, library_id=zot.library_id, smart_fill=smart_fill,
                      field_sources=field_sources)
    return json.dumps({"drifted": plan.drifted, "drift_keys": plan.drift_keys,
                       "patches": plan.patches, "master_version": plan.master_version})


@mcp.tool()
def commit_merge(master_key: str, snapshot_id: str, smart_fill: bool = False,
                 field_sources: Optional[dict] = None, expected_master_version: Optional[int] = None) -> str:
    """Verify-gated commit: re-run the 11-check verify against the live post-PATCH state, then TRASH the
    secondaries (PATCH deleted:1, NEVER purge). SHADOW by default — it only trashes when the out-of-band
    env token ZOT_MERGE_LIVE_ENABLED is set AND observability is fresh AND the ceiling/disjointness gates
    hold. Any post-verify failure routes to rollback_merge.

    `field_sources` MUST match the merge_cluster call (verify check #3 enforces the projected survivor).
    `expected_master_version` = the `master_version` merge_cluster returned; if the live master has
    advanced past it (a concurrent edit landed in the merge_cluster→commit window) the commit fails closed
    and rolls back rather than trashing on top of someone else's edit (review #7 concurrency pin). When
    omitted (None) the pin is skipped — the Python live-apply driver passes it; the agent surface should
    thread merge_cluster's returned master_version straight through."""
    zot = get_client()
    snap = _eng_load_snapshot(zot.prov, snapshot_id)
    if snap is None:
        return json.dumps({"error": f"unknown snapshot_id {snapshot_id}"})
    reader = WebClusterReader(zot, zot.library_id)
    res = _eng_commit(snap, reader, zot.gateway, zot.prov, library_id=zot.library_id, smart_fill=smart_fill,
                      field_sources=field_sources, expected_master_version=expected_master_version)
    return json.dumps({"mode": res.mode, "reason": res.reason, "verify_passed": res.verify_passed,
                       "trashed": res.trashed,
                       "rollback_ok": (res.rollback.ok if res.rollback else None)})


@mcp.tool()
def rollback_merge(snapshot_id: str) -> str:
    """Undo a merge from its snapshot: un-trash secondaries + revert the master + reparent children to
    their original parents. `ok` is False if a restore op itself failed (escalate to human recovery)."""
    zot = get_client()
    snap = _eng_load_snapshot(zot.prov, snapshot_id)
    if snap is None:
        return json.dumps({"error": f"unknown snapshot_id {snapshot_id}"})
    reader = WebClusterReader(zot, zot.library_id)
    observed = _eng_build(reader, snap.master_key, list(snap.secondary_keys))
    rb = _eng_rollback(snap, observed, zot.gateway, library_id=zot.library_id)
    return json.dumps({"state": rb.state, "ok": rb.ok, "operations": rb.operations, "failures": rb.failures})


@mcp.tool()
def reconcile_orphans() -> str:
    """F4 crash-recovery on demand: find every orphaned `commit_merge_intent` (a live merge that crashed
    mid-trash — intent logged, no result) and roll it back from its snapshot (un-trash secondaries +
    revert master + reparent children — the sanctioned rollback path, never purge). This ALSO runs once
    automatically at server startup. Returns structured counts; any orphan whose snapshot blob is MISSING
    is reported LOUDLY under `no_snapshot_blob` with an `alert` (non-recoverable — needs human recovery),
    never a silent skip."""
    zot = get_client()
    reader = WebClusterReader(zot, zot.library_id)
    outcomes = _eng_reconcile(zot.prov, reader, zot.gateway, library_id=zot.library_id)
    no_blob = [o.get("snapshot_id") for o in outcomes if o.get("status") == "no-snapshot-blob"]
    failed = [o.get("snapshot_id") for o in outcomes if o.get("status") == "rollback_failed"]
    alert = None
    if no_blob or failed:
        alert = (f"{len(no_blob)} orphan(s) have NO snapshot blob (non-recoverable) and "
                 f"{len(failed)} rollback(s) failed — HUMAN REVIEW required")
    return json.dumps({
        "orphans_found": len(outcomes),
        "reconciled": sum(1 for o in outcomes if o.get("status") == "reconciled"),
        "rollback_failed": failed,
        "no_snapshot_blob": no_blob,
        "alert": alert,
        "outcomes": [{"snapshot_id": o.get("snapshot_id"), "status": o.get("status")} for o in outcomes],
    })


@mcp.tool()
def dedup_scan(item_keys: list, review_threshold: float = 0.99) -> str:
    """Deterministic duplicate clustering over the given items. `auto_accept` fires ONLY on the ASySD
    boolean (exact DOI OR exact normalized title+year+first-author) with item-type/title/DOI conflict
    guards; probabilistic scoring is a deferred review-queue-only seam. Never commits."""
    zot = get_client()
    items = [zot.get_item_web(k) for k in item_keys]
    res = _eng_dedup(items, review_threshold=review_threshold)
    clusters = [{"item_keys": c.item_keys, "auto_accept": c.auto_accept, "master_key": c.master_key,
                 "reason": c.reason, "conflicts": c.conflicts} for c in res["candidate_clusters"]]
    return json.dumps({"candidate_clusters": clusters, "auto_accept_count": res["auto_accept_count"],
                       "review_count": res["review_count"], "probabilistic_review": res["probabilistic_review"]})


@mcp.tool()
def query_provenance(item_key: str) -> str:
    """The full PROV history for an item — every mutation with its before/after sha256 + the per-record
    reversibility index (snapshot_id / reverse / blobs)."""
    return json.dumps(_eng_query_prov(get_client().prov, item_key), default=str)


@mcp.tool()
def merge_health_report() -> str:
    """Compute + record the daily merge-health marker (verify-pass-rate, merges-committed, sampled audit).
    The marker gates live commits — commit_merge refuses on a stale or below-floor (degraded) report."""
    return json.dumps(_eng_daily(get_client().prov), default=str)


# ═══════════════════════════════════════════════════════════════════
#  S5a — PHASE-6 READ-ONLY TOOLING (dashboard + doctor support; NEVER a Zotero mutation)
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
def prov_coverage_report(recent_n: int = 20) -> str:
    """S5a F2: the owner-facing "did every mutation get audited?" answer (ADR-008 interlock — PROV
    IS the rollback index). Read-only aggregation over the whole append-only PROV log: total record
    count, a per-activity breakdown, the verify-pass-rate (same computation `daily_report` uses, incl.
    the OBS-5 acknowledge exclusion), and the last `recent_n` merges with their before/after sha256 for
    a spot-check. Makes NO write of any kind — unlike merge_health_report, not even a marker append."""
    return json.dumps(_eng_prov_coverage(get_client().prov, recent_n=recent_n), default=str)


@mcp.tool()
def preview_merge(master_key: str, dup_keys: list, field_sources: Optional[dict] = None,
                  smart_fill: bool = False) -> str:
    """S5a F6: read-only "what would this merge do" preview. Reuses `shadow_merge`
    (snapshot -> compute_merge_projection -> verify_merge -> log) — structurally read-only, takes NO
    gateway, cannot write to the library. Returns the survivor field changes, which of the 11 verify
    checks pass, the post-merge collections/tags, and the trash-would-be set. Owner ergonomics for
    inspecting one merge before committing; also what the human-review queue and a supervised live
    cycle preview a cluster with before its one live commit."""
    zot = get_client()
    reader = WebClusterReader(zot, zot.library_id)
    sm_before = reader.get_item(master_key).get("data", {})
    base = library_item_base("user", zot.library_id)
    sr = _eng_shadow_merge(reader, master_key, list(dup_keys), prov=zot.prov, smart_fill=smart_fill,
                           field_sources=field_sources, library_base=base)
    survivor = sr.projection.items[master_key]
    watch_fields = set(list(field_sources or {}) + ["extra"])
    changes = {f: {"from": sm_before.get(f), "to": survivor.fields.get(f)}
              for f in watch_fields if survivor.fields.get(f) != sm_before.get(f)}
    return json.dumps({
        "snapshot_id": sr.snapshot_id,
        "verify_pass": sr.passed,
        "checks": [{"number": c.number, "name": c.name, "pass": c.passed, "detail": c.detail}
                   for c in sr.integrity.checks],
        "survivor_changes": changes,
        "trash_would_be": list(dup_keys),
        "collections_after": survivor.collections,
        "tags_after": survivor.tags,
    })


@mcp.tool()
def citekey_audit_report(check_aliases: bool = True) -> str:
    """S5a F7: read-only, whole-library citekey-collision + tex.ids alias-survival sweep. (a) Groups
    every live item's BBT citekey (pinned `extra` Citation Key: line, else `citationKey`) and reports
    any duplicate (a collision silently breaks the downstream Pandoc @citekey pipeline). (b) If
    `check_aliases`, for every live item carrying a `dc:replaces` relation (a merge survivor), confirms
    each trashed target's own citekey survives as a `tex.ids:` alias on the survivor's `extra` — an
    individual GET per dc:replaces pair (currently ~151 from the S2 mass merge), so this can take a
    while; pass `check_aliases=False` for the collision-only pass. Paged via the Web API (never the
    local API), so it is unaffected by local-API host-health degradation."""
    zot = get_client()
    items = _eng_web_items(zot)
    collisions = _eng_citekeys.scan_citekey_collisions(items)
    aliases = None
    if check_aliases:
        def _lookup(key: str):
            try:
                return zot.get_item_web(key)
            except Exception:
                return None
        aliases = _eng_citekeys.scan_tex_ids_aliases(items, _lookup)
    return json.dumps({"collisions": collisions, "tex_ids_aliases": aliases}, default=str)


@mcp.tool()
def batch_validate_items(
    limit: int = 50,
    start: int = 0,
    item_type: Optional[str] = None,
) -> str:
    """Audit multiple library items for completeness and formatting issues.

    Scans items and reports those with missing required fields, formatting
    problems, or other quality issues.

    Args:
        limit: Number of items to scan
        start: Starting offset for pagination
        item_type: Optional filter by item type (e.g. "journalArticle")
    """
    zot = get_client()

    itype_filter = item_type if item_type else "-attachment"
    items = zot.get_all_items(limit=limit, start=start, item_type=itype_filter)

    results = {"clean": 0, "issues": [], "warnings_only": []}

    for item in items:
        data = item.get("data", item)
        itype = data.get("itemType", "")
        key = item.get("key", data.get("key", ""))
        issues = []
        warnings = []

        if not data.get("title"):
            issues.append("no title")
        if not data.get("creators"):
            issues.append("no creators")
        if not data.get("date"):
            issues.append("no date")

        if itype == "journalArticle":
            if not data.get("publicationTitle"):
                issues.append("no journal")
            if not data.get("DOI"):
                warnings.append("no DOI")
        elif itype == "book":
            if not data.get("publisher"):
                issues.append("no publisher")
        elif itype == "bookSection":
            if not data.get("bookTitle"):
                issues.append("no book title")

        if issues:
            results["issues"].append((key, format_item_summary(item), issues, warnings))
        elif warnings:
            results["warnings_only"].append((key, format_item_summary(item), warnings))
        else:
            results["clean"] += 1

    lines = [f"**Library Audit** — scanned {len(items)} items\n"]
    lines.append(f"✅ Clean: {results['clean']}")
    lines.append(f"❌ Issues: {len(results['issues'])}")
    lines.append(f"⚠️ Warnings only: {len(results['warnings_only'])}")

    if results["issues"]:
        lines.append("\n### Items with Issues\n")
        for key, summary, issues, warnings in results["issues"][:25]:
            lines.append(f"**{summary}**")
            lines.append(f"  Issues: {', '.join(issues)}")
            if warnings:
                lines.append(f"  Warnings: {', '.join(warnings)}")

    if results["warnings_only"][:10]:
        lines.append("\n### Items with Warnings Only\n")
        for key, summary, warnings in results["warnings_only"][:10]:
            lines.append(f"**{summary}**")
            lines.append(f"  {', '.join(warnings)}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  FILE ATTACHMENT & CORPUS LINKING TOOLS
# ═══════════════════════════════════════════════════════════════════

from zotero_write_mcp.fileops import (
    get_content_type,
    scan_directory,
    extract_doi_from_pdf,
    extract_metadata_from_markdown,
    extract_metadata_from_filename,
)
from pathlib import Path


@mcp.tool()
def attach_file_linked(
    item_key: str,
    file_path: str,
    title: Optional[str] = None,
) -> str:
    """DISABLED (S0 C.5) — imported-only attachment policy (.claude/rules/file-handling.md).

    Linked-file attachments store only a local path: they work ONLY on the machine holding the file and
    do NOT sync to cloud / groups / web / mobile. This tool now HARD-REFUSES at the engine so no caller
    (raw MCP, stand-alone mode A, or a misconfigured host) can create a non-syncing linked attachment —
    closing the bypass that the harness deny-list previously guarded only at the control-plane layer.
    Use ``attach_file_imported`` instead (imported files sync and are accessible from any device).
    """
    return (
        "attach_file_linked is DISABLED (imported-only policy — .claude/rules/file-handling.md). Linked "
        "attachments do not sync (cloud/groups/web/mobile) and only work on the machine holding the file. "
        "Use attach_file_imported instead."
    )


@mcp.tool()
def attach_file_imported(
    item_key: str,
    file_path: str,
    title: Optional[str] = None,
) -> str:
    """Attach a file to a Zotero item as an IMPORTED file (Zotero copies the file into its storage).

    A copy of the file is stored in Zotero's data directory.
    Best for: ensuring Zotero has its own copy, portable libraries, sync across devices.

    Args:
        item_key: The parent Zotero item key to attach the file to
        file_path: Absolute path to the source file on disk
        title: Display title for the attachment (defaults to filename)
    """
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        return f"❌ File not found: {file_path}"

    zot = get_client()

    try:
        item = zot.get_item(item_key)
    except Exception as e:
        return f"❌ Could not find item {item_key}: {e}"

    fname = title or Path(file_path).name
    ctype = get_content_type(file_path)
    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    result = zot.create_imported_file_attachment(item_key, file_path, fname, ctype)
    success = result.get("success", {})
    uploaded = result.get("uploaded", False)

    if success:
        att_key = list(success.values())[0]
        upload_status = "uploaded" if uploaded else "created (upload may need Zotero sync)"
        return (
            f"✅ Imported file attachment {upload_status}.\n"
            f"  Parent: {item_key}\n"
            f"  Attachment key: {att_key}\n"
            f"  Source: {file_path}\n"
            f"  Size: {size_mb:.1f} MB\n"
            f"  Type: {ctype}"
        )
    else:
        return f"❌ Failed to create attachment: {json.dumps(result)}"


@mcp.tool()
def check_item_attachments(
    item_key: str,
) -> str:
    """List all attachments (files, links) for a Zotero item.

    Args:
        item_key: The Zotero item key
    """
    zot = get_client()
    item = zot.get_item(item_key)
    children = zot.get_item_children_web(item_key)  # web read for current state

    attachments = [
        c for c in children
        if c.get("data", c).get("itemType") == "attachment"
    ]

    if not attachments:
        return f"ℹ️ Item {item_key} has no attachments."

    lines = [f"**Attachments for {item_key}** ({len(attachments)}):\n"]
    for att in attachments:
        data = att.get("data", att)
        key = att.get("key", "?")
        link_mode = data.get("linkMode", "?")
        title = data.get("title", "Untitled")
        path = data.get("path", "")
        ctype = data.get("contentType", "")
        filename = data.get("filename", "")

        lines.append(f"  **[{key}]** {title}")
        lines.append(f"    Mode: {link_mode} | Type: {ctype}")
        if path:
            exists = os.path.isfile(path) if link_mode == "linked_file" else "N/A"
            lines.append(f"    Path: {path}")
            if link_mode == "linked_file":
                lines.append(f"    File exists: {'✅ Yes' if exists else '❌ No (broken link)'}")
        if filename:
            lines.append(f"    Filename: {filename}")

    return "\n".join(lines)


@mcp.tool()
def scan_directory_for_sources(
    directory: str,
    extensions: Optional[list[str]] = None,
    recursive: bool = True,
    max_files: int = 500,
    match_against_library: bool = True,
    match_threshold: float = 0.55,
) -> str:
    """Scan a directory of PDF/MD files, extract metadata, and optionally match against the Zotero library.

    For each file, attempts to extract identifiers (DOI from PDFs, frontmatter from MDs,
    author/year/title from filenames) and searches the library for matches.

    Results are sorted into three buckets:
    - **Confident matches** (score >= 0.7): likely already in library
    - **Uncertain matches** (score >= threshold): need human review
    - **No match**: new items that could be added

    Args:
        directory: Directory path to scan
        extensions: File extensions to include (default: [".pdf", ".md"])
        recursive: Scan subdirectories (default True)
        max_files: Maximum files to process (default 500)
        match_against_library: Whether to search Zotero for matches (default True)
        match_threshold: Minimum score to consider a match (default 0.55)
    """
    if extensions is None:
        extensions = [".pdf", ".md"]

    if not os.path.isdir(directory):
        return f"❌ Directory not found: {directory}"

    files = scan_directory(directory, extensions, recursive, max_files)

    if not files:
        return f"ℹ️ No matching files found in {directory}"

    zot = get_client() if match_against_library else None

    confident_matches = []
    uncertain_matches = []
    no_match = []
    errors = []

    for finfo in files:
        fpath = finfo["path"]
        ext = finfo["extension"]

        # Extract metadata based on file type
        extracted = {"title": "", "authors": [], "date": "", "doi": ""}
        try:
            if ext == ".pdf":
                doi = extract_doi_from_pdf(fpath)
                if doi:
                    extracted["doi"] = doi
                fn_meta = extract_metadata_from_filename(finfo["filename"])
                extracted.update({k: v for k, v in fn_meta.items() if v})
            elif ext in (".md", ".markdown"):
                md_meta = extract_metadata_from_markdown(fpath)
                extracted.update({k: v for k, v in md_meta.items() if v})
                if not extracted["title"]:
                    fn_meta = extract_metadata_from_filename(finfo["filename"])
                    extracted.update({k: v for k, v in fn_meta.items() if v})
            else:
                fn_meta = extract_metadata_from_filename(finfo["filename"])
                extracted.update({k: v for k, v in fn_meta.items() if v})
        except Exception as e:
            errors.append((finfo["filename"], str(e)))
            continue

        finfo["extracted"] = extracted

        # Match against library
        if zot and (extracted["title"] or extracted["doi"]):
            best_score = 0.0
            best_match = None

            # DOI search first
            if extracted["doi"]:
                try:
                    results = zot.search_items(extracted["doi"], limit=3, qmode="everything")
                    for r in results:
                        r_doi = r.get("data", {}).get("DOI", "").strip().lower()
                        if r_doi and r_doi == extracted["doi"].strip().lower():
                            best_score = 1.0
                            best_match = r
                            break
                except Exception:
                    pass

            # Title search
            if best_score < 0.7 and extracted["title"]:
                try:
                    query = extracted["title"][:80]
                    results = zot.search_items(query, limit=5)
                    for r in results:
                        # Build a pseudo-item for comparison
                        pseudo = {"data": {
                            "title": extracted["title"],
                            "creators": [{"creatorType": "author", "lastName": a}
                                         for a in extracted.get("authors", [])],
                            "date": extracted.get("date", ""),
                        }}
                        score = compute_duplicate_score(pseudo, r)
                        if score > best_score:
                            best_score = score
                            best_match = r
                except Exception:
                    pass

            if best_match and best_score >= 0.7:
                confident_matches.append((finfo, best_score, best_match))
            elif best_match and best_score >= match_threshold:
                uncertain_matches.append((finfo, best_score, best_match))
            else:
                no_match.append(finfo)
        else:
            no_match.append(finfo)

    # Format results
    lines = [
        f"## Directory Scan Results\n",
        f"**Scanned:** {directory}",
        f"**Files found:** {len(files)}",
        f"**Extensions:** {', '.join(extensions)}\n",
    ]

    if match_against_library:
        lines.append(f"### ✅ Confident Matches ({len(confident_matches)})\n")
        if confident_matches:
            lines.append("These files likely correspond to existing library entries:\n")
            for finfo, score, match in confident_matches[:30]:
                lines.append(f"  **{finfo['filename']}** → {score:.0%} match")
                lines.append(f"    Library: {format_item_summary(match)}")
                if finfo["extracted"].get("doi"):
                    lines.append(f"    DOI: {finfo['extracted']['doi']}")
                lines.append("")
        else:
            lines.append("  None\n")

        lines.append(f"### ⚠️ Uncertain Matches ({len(uncertain_matches)})\n")
        if uncertain_matches:
            lines.append("Need human review:\n")
            for finfo, score, match in uncertain_matches[:30]:
                lines.append(f"  **{finfo['filename']}** → {score:.0%} match")
                lines.append(f"    Library: {format_item_summary(match)}")
                lines.append("")
        else:
            lines.append("  None\n")

        lines.append(f"### 🆕 No Match ({len(no_match)})\n")
        if no_match:
            lines.append("Not found in library (could be added as new entries):\n")
            for finfo in no_match[:50]:
                ext_meta = finfo.get("extracted", {})
                lines.append(f"  **{finfo['filename']}**")
                if ext_meta.get("title"):
                    lines.append(f"    Extracted title: {ext_meta['title']}")
                if ext_meta.get("doi"):
                    lines.append(f"    DOI: {ext_meta['doi']}")
                lines.append("")
        else:
            lines.append("  None\n")

    else:
        lines.append(f"### Files Found ({len(files)})\n")
        for finfo in files[:50]:
            ext_meta = finfo.get("extracted", {})
            lines.append(f"  **{finfo['filename']}** ({finfo['size_bytes'] / 1024:.0f} KB)")
            if ext_meta.get("title"):
                lines.append(f"    Title: {ext_meta['title']}")
            if ext_meta.get("doi"):
                lines.append(f"    DOI: {ext_meta['doi']}")
            lines.append("")

    if errors:
        lines.append(f"\n### ⚠️ Errors ({len(errors)})\n")
        for fname, err in errors[:10]:
            lines.append(f"  {fname}: {err}")

    if len(files) > 50:
        lines.append(f"\n*Showing first 50 results. {len(files) - 50} more files not displayed.*")

    lines.append(
        "\nUse `attach_file_imported` to attach files to items (imported-only — linked attachments do "
        "not sync). Use `bulk_link_files` to batch-attach confident matches (imported)."
    )

    return "\n".join(lines)


@mcp.tool()
def bulk_link_files(
    mappings: list[dict],
    mode: str = "imported",
    confirm: bool = False,
) -> str:
    """Batch-attach multiple files to their matching Zotero items (IMPORTED-only).

    This is a bulk operation. By default returns a preview; set confirm=True to execute.

    Args:
        mappings: List of dicts, each with:
                  - 'file_path': absolute path to the file
                  - 'item_key': Zotero item key to attach to
                  - 'title' (optional): display title for the attachment
        mode: "imported" (default AND the only accepted value) — Zotero-managed copies that sync to
              cloud/groups/web/mobile. `mode="linked"` is REJECTED (S0 C.5, imported-only policy —
              .claude/rules/file-handling.md); linked attachments do not sync and only work on the
              machine holding the file. The safe choice is now the default (poka-yoke).
        confirm: Must be True to execute. False returns a preview.
    """
    if not mappings:
        return "❌ No mappings provided."

    if mode != "imported":
        return ("❌ Invalid mode '" + str(mode) + "'. Only 'imported' is allowed — linked attachments do "
                "not sync (cloud/groups/web/mobile) and only work on the machine holding the file. See "
                ".claude/rules/file-handling.md.")

    # Validate all files exist
    missing = []
    valid = []
    for m in mappings:
        fp = m.get("file_path", "")
        ik = m.get("item_key", "")
        if not fp or not ik:
            missing.append(f"Invalid mapping (missing file_path or item_key): {m}")
            continue
        if not os.path.isfile(fp):
            missing.append(f"File not found: {fp}")
            continue
        valid.append(m)

    if not confirm or (requires_confirmation(RiskLevel.HIGH) and not confirm):
        lines = [
            f"## Bulk File Attach Preview\n",
            f"**Mode:** {mode}",
            f"**Total mappings:** {len(mappings)}",
            f"**Valid:** {len(valid)}",
            f"**Missing/invalid:** {len(missing)}\n",
        ]

        if valid:
            lines.append("### Files to Attach:\n")
            for m in valid[:30]:
                size = os.path.getsize(m["file_path"]) / 1024
                lines.append(f"  {Path(m['file_path']).name} ({size:.0f} KB) → [{m['item_key']}]")

        if missing:
            lines.append("\n### Issues:\n")
            for msg in missing:
                lines.append(f"  ❌ {msg}")

        total_size = sum(os.path.getsize(m["file_path"]) for m in valid) / (1024 * 1024)
        lines.append(f"\n**Total size:** {total_size:.1f} MB")
        if mode == "imported":
            lines.append(f"⚠️ Imported mode will COPY all files into Zotero storage.")

        lines.append(f"\nCall with `confirm=True` to execute.")
        return "\n".join(lines)

    # Execute
    zot = get_client()
    results = {"success": 0, "failed": 0, "skipped": 0, "errors": []}

    for m in valid:
        fp = m["file_path"]
        ik = m["item_key"]
        title = m.get("title", Path(fp).name)
        ctype = get_content_type(fp)

        try:
            # Check if already attached
            children = zot.get_item_children_web(ik)
            already_attached = False
            for child in children:
                cd = child.get("data", child)
                if cd.get("path", "") == fp or cd.get("filename", "") == Path(fp).name:
                    already_attached = True
                    break

            if already_attached:
                results["skipped"] += 1
                continue

            result = zot.create_imported_file_attachment(ik, fp, title, ctype)   # imported-only (S0 C.5)

            if result.get("success"):
                results["success"] += 1
            else:
                results["failed"] += 1
                results["errors"].append(f"{Path(fp).name}: {json.dumps(result)}")
        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"{Path(fp).name}: {e}")

    lines = [
        f"## Bulk Attach Complete\n",
        f"✅ Success: {results['success']}",
        f"⏭️ Skipped (already attached): {results['skipped']}",
        f"❌ Failed: {results['failed']}",
    ]
    if results["errors"]:
        lines.append("\n### Errors:\n")
        for err in results["errors"][:20]:
            lines.append(f"  - {err}")

    return "\n".join(lines)
