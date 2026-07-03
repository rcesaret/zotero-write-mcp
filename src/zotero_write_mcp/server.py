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
)
from zotero_write_mcp.merge_live import (
    merge_cluster as _eng_merge, commit_merge as _eng_commit, load_snapshot as _eng_load_snapshot,
    reconcile_orphan_commits as _eng_reconcile, WebClusterReader,
)
from zotero_write_mcp.dedup import dedup_scan as _eng_dedup
from zotero_write_mcp.observability import query_provenance as _eng_query_prov, daily_report as _eng_daily

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

    zot = get_client()

    # Safety check
    if requires_confirmation(RiskLevel.HIGH) and not confirm:
        # Build preview
        item_a = zot.get_item(primary_key)
        item_b = zot.get_item(secondary_key)
        data_a = item_a.get("data", item_a)
        data_b = item_b.get("data", item_b)

        lines = [
            "## Merge Preview\n",
            f"**Primary (keep):** {format_item_summary(item_a)}",
            f"**Secondary (delete):** {format_item_summary(item_b)}",
            "",
        ]

        if use_secondary_fields:
            lines.append(f"**Fields taken from secondary:** {', '.join(use_secondary_fields)}")
            for f in use_secondary_fields:
                lines.append(f"  {f}: '{data_b.get(f, '')}' ← replaces '{data_a.get(f, '')}'")

        if field_overrides:
            lines.append(f"**Explicit overrides:** {json.dumps(field_overrides)}")

        if merge_tags:
            tags_a = {t.get("tag", "") for t in data_a.get("tags", [])}
            tags_b = {t.get("tag", "") for t in data_b.get("tags", [])}
            combined = tags_a | tags_b
            lines.append(f"**Merged tags:** {', '.join(sorted(combined))}")

        lines.append(
            f"\n⚠️ **This will DELETE item {secondary_key}.** "
            "Call again with `confirm=True` to execute."
        )
        return "\n".join(lines)

    # Execute merge
    item_a = zot.get_item_web(primary_key)  # web read for current version
    item_b = zot.get_item_web(secondary_key)  # web read for current version
    data_a = item_a.get("data", item_a)
    data_b = item_b.get("data", item_b)
    version_a = item_a.get("version", data_a.get("version"))
    version_b = item_b.get("version", data_b.get("version"))

    # Apply secondary fields
    if use_secondary_fields:
        for field in use_secondary_fields:
            if field in data_b:
                data_a[field] = data_b[field]

    # Merge tags
    if merge_tags:
        tags_a = data_a.get("tags", [])
        tags_b = data_b.get("tags", [])
        existing_tags = {t["tag"] for t in tags_a}
        for t in tags_b:
            if t["tag"] not in existing_tags:
                tags_a.append(t)
        data_a["tags"] = tags_a

    # Apply explicit overrides (highest priority)
    if field_overrides:
        for k, v in field_overrides.items():
            data_a[k] = v

    # Update primary
    zot.update_item(primary_key, data_a, version_a)

    # Delete secondary
    try:
        zot.delete_item(secondary_key, version_b)
        return (
            f"✅ Merge complete.\n"
            f"**Kept:** {primary_key}\n"
            f"**Deleted:** {secondary_key}"
        )
    except Exception as e:
        return (
            f"⚠️ Primary item {primary_key} updated, but failed to delete "
            f"secondary {secondary_key}: {e}\n"
            f"You may need to delete it manually."
        )


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
def merge_cluster(master_key: str, dup_keys: list, snapshot_id: str, smart_fill: bool = False) -> str:
    """PATCH phase of a merge: reparent children to the master + union collections/tags/relations +
    dc:replaces. NO delete — reversible via rollback_merge. Aborts with NO writes on version drift."""
    zot = get_client()
    snap = _eng_load_snapshot(zot.prov, snapshot_id)
    if snap is None:
        return json.dumps({"error": f"unknown snapshot_id {snapshot_id}; call snapshot_cluster first"})
    reader = WebClusterReader(zot, zot.library_id)
    plan = _eng_merge(snap, reader, zot.gateway, library_id=zot.library_id, smart_fill=smart_fill)
    return json.dumps({"drifted": plan.drifted, "drift_keys": plan.drift_keys,
                       "patches": plan.patches, "master_version": plan.master_version})


@mcp.tool()
def commit_merge(master_key: str, snapshot_id: str, smart_fill: bool = False) -> str:
    """Verify-gated commit: re-run the 11-check verify against the live post-PATCH state, then TRASH the
    secondaries (PATCH deleted:1, NEVER purge). SHADOW by default — it only trashes when the out-of-band
    env token ZOT_MERGE_LIVE_ENABLED is set AND observability is fresh AND the ceiling/disjointness gates
    hold. Any post-verify failure routes to rollback_merge."""
    zot = get_client()
    snap = _eng_load_snapshot(zot.prov, snapshot_id)
    if snap is None:
        return json.dumps({"error": f"unknown snapshot_id {snapshot_id}"})
    reader = WebClusterReader(zot, zot.library_id)
    res = _eng_commit(snap, reader, zot.gateway, zot.prov, library_id=zot.library_id, smart_fill=smart_fill)
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
    """Attach a file to a Zotero item as a LINKED file (Zotero points to the file on disk).

    The file stays where it is. Zotero stores only the path reference.
    Best for: existing organized file collections you don't want duplicated.

    Args:
        item_key: The parent Zotero item key to attach the file to
        file_path: Absolute path to the file on disk
        title: Display title for the attachment (defaults to filename)
    """
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        return f"❌ File not found: {file_path}"

    zot = get_client()

    # Check item exists
    try:
        item = zot.get_item(item_key)
    except Exception as e:
        return f"❌ Could not find item {item_key}: {e}"

    # Check if already attached
    children = zot.get_item_children_web(item_key)
    for child in children:
        child_data = child.get("data", child)
        if child_data.get("path", "") == file_path:
            return (
                f"ℹ️ File already linked to {item_key}:\n"
                f"  Path: {file_path}\n"
                f"  Attachment key: {child.get('key', '?')}"
            )

    fname = title or Path(file_path).name
    ctype = get_content_type(file_path)

    result = zot.create_linked_file_attachment(item_key, file_path, fname, ctype)
    success = result.get("success", {})
    if success:
        att_key = list(success.values())[0]
        return (
            f"✅ Linked file attachment created.\n"
            f"  Parent: {item_key}\n"
            f"  Attachment key: {att_key}\n"
            f"  Path: {file_path}\n"
            f"  Type: {ctype}"
        )
    else:
        return f"❌ Failed to create attachment: {json.dumps(result)}"


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
        "\nUse `attach_file_linked` or `attach_file_imported` to attach files to items.\n"
        "Use `bulk_link_files` to batch-attach confident matches."
    )

    return "\n".join(lines)


@mcp.tool()
def bulk_link_files(
    mappings: list[dict],
    mode: str = "linked",
    confirm: bool = False,
) -> str:
    """Batch-attach multiple files to their matching Zotero items.

    This is a bulk operation. By default returns a preview; set confirm=True to execute.

    Args:
        mappings: List of dicts, each with:
                  - 'file_path': absolute path to the file
                  - 'item_key': Zotero item key to attach to
                  - 'title' (optional): display title for the attachment
        mode: "linked" (default) for linked files, "imported" for Zotero-managed copies
        confirm: Must be True to execute. False returns a preview.
    """
    if not mappings:
        return "❌ No mappings provided."

    if mode not in ("linked", "imported"):
        return f"❌ Invalid mode '{mode}'. Use 'linked' or 'imported'."

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

            if mode == "linked":
                result = zot.create_linked_file_attachment(ik, fp, title, ctype)
            else:
                result = zot.create_imported_file_attachment(ik, fp, title, ctype)

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
