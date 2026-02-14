# zotero-write-mcp — API Reference

Complete reference for all 18 tools exposed by the `zotero-write-mcp` MCP server (v0.1.1).

---

## Table of Contents

- [Creation Tools](#creation-tools)
  - [create_item](#create_item)
  - [create_item_from_doi](#create_item_from_doi)
  - [create_item_from_bibtex](#create_item_from_bibtex)
- [Editing Tools](#editing-tools)
  - [update_item_fields](#update_item_fields)
  - [add_tags_to_item](#add_tags_to_item)
  - [remove_tags_from_item](#remove_tags_from_item)
  - [validate_item](#validate_item)
  - [get_item_type_fields](#get_item_type_fields)
- [Merging & Audit Tools](#merging--audit-tools)
  - [find_duplicate_candidates](#find_duplicate_candidates)
  - [find_duplicates_for_item](#find_duplicates_for_item)
  - [compare_items_for_merge](#compare_items_for_merge)
  - [merge_items](#merge_items)
  - [batch_validate_items](#batch_validate_items)
- [File Operations Tools](#file-operations-tools)
  - [attach_file_linked](#attach_file_linked)
  - [attach_file_imported](#attach_file_imported)
  - [check_item_attachments](#check_item_attachments)
  - [scan_directory_for_sources](#scan_directory_for_sources)
  - [bulk_link_files](#bulk_link_files)

---

## Creation Tools

### `create_item`

Create a new Zotero item from explicit metadata fields. Supports optional duplicate checking.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_type` | `str` | **Yes** | — | Zotero item type: `journalArticle`, `book`, `bookSection`, `conferencePaper`, `thesis`, `report`, etc. |
| `title` | `str` | **Yes** | — | Item title |
| `creators` | `list[dict]` | **Yes** | — | List of creator dicts, each with `creatorType`, `firstName`, `lastName`. Example: `[{"creatorType": "author", "firstName": "John", "lastName": "Smith"}]` |
| `date` | `str` | No | `""` | Publication date (e.g. `"2024"`, `"2024-03-15"`) |
| `publication_title` | `str` | No | `""` | Journal or publication name |
| `volume` | `str` | No | `""` | Volume number |
| `issue` | `str` | No | `""` | Issue number |
| `pages` | `str` | No | `""` | Page range (e.g. `"1-25"`) |
| `doi` | `str` | No | `""` | Digital Object Identifier |
| `publisher` | `str` | No | `""` | Publisher name |
| `place` | `str` | No | `""` | Place of publication |
| `url` | `str` | No | `""` | URL |
| `abstract` | `str` | No | `""` | Abstract text |
| `isbn` | `str` | No | `""` | ISBN |
| `issn` | `str` | No | `""` | ISSN |
| `book_title` | `str` | No | `""` | Book title (for `bookSection` items) |
| `edition` | `str` | No | `""` | Edition |
| `series` | `str` | No | `""` | Series name |
| `extra_fields` | `dict` | No | `None` | Additional Zotero fields as key-value pairs |
| `tags` | `list[str]` | No | `None` | List of tag strings to apply |
| `check_duplicates` | `bool` | No | `True` | If True, search for duplicates before creating |

**Behavior:**
- When `check_duplicates=True`, searches by DOI (exact match) and title (fuzzy match, >60% threshold).
- If duplicates found, returns a warning with matches and does NOT create the item.
- Set `check_duplicates=False` to force creation regardless.

**Returns:** Success message with new item key, or duplicate warning, or error.

---

### `create_item_from_doi`

Create a new Zotero item by resolving metadata from a DOI via the Crossref API.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `doi` | `str` | **Yes** | — | The DOI to look up (e.g. `"10.1016/j.jas.2005.10.003"`) |
| `tags` | `list[str]` | No | `None` | Optional tags to apply |
| `check_duplicates` | `bool` | No | `True` | Check for existing entries before creating |

**Behavior:**
1. Resolves DOI via Crossref API to get full metadata (title, authors, journal, date, etc.)
2. If `check_duplicates=True`, checks library for existing items with same DOI or similar title.
3. Creates the item via Web API if no duplicates found.

**Returns:** Success with new item key, duplicate warning, or DOI resolution error.

---

### `create_item_from_bibtex`

Create a new Zotero item by parsing a BibTeX entry string.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `bibtex` | `str` | **Yes** | — | A BibTeX entry string (e.g. `@article{key, title={...}, ...}`) |
| `tags` | `list[str]` | No | `None` | Optional tags to apply |
| `check_duplicates` | `bool` | No | `True` | Check for existing entries before creating |

**Behavior:**
- Parses BibTeX using `bibtexparser`, maps fields to Zotero schema.
- BibTeX types mapped: `article`→`journalArticle`, `book`→`book`, `inbook`/`incollection`→`bookSection`, `inproceedings`→`conferencePaper`, `phdthesis`/`mastersthesis`→`thesis`, etc.

---

## Editing Tools

### `update_item_fields`

Update specific fields on an existing Zotero item.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The Zotero item key (e.g. `"BCBLBCMI"`) |
| `fields` | `dict` | **Yes** | — | Dictionary of field names and new values |

**Supported fields:** `title`, `date`, `publicationTitle`, `volume`, `issue`, `pages`, `DOI`, `publisher`, `place`, `url`, `abstractNote`, `ISBN`, `ISSN`, `bookTitle`, `edition`, `series`, `extra`, `rights`, `language`, etc. For `creators`, pass a full creators array. For `tags`, pass a list of `{"tag": "name"}` dicts.

**Behavior:**
- Fetches current item from Web API (for accurate version number).
- Applies field updates and pushes back via Web API PATCH.
- Handles 412 Precondition Failed with automatic retry.

---

### `add_tags_to_item`

Add tags to an existing Zotero item without removing existing tags.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The Zotero item key |
| `tags` | `list[str]` | **Yes** | — | List of tag strings to add |

**Behavior:** Merges new tags with existing. Skips tags already present. Reports how many were added.

---

### `remove_tags_from_item`

Remove specific tags from a Zotero item.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The Zotero item key |
| `tags` | `list[str]` | **Yes** | — | List of tag strings to remove |

**Behavior:** Filters out specified tags. Reports how many were removed.

---

### `validate_item`

Check a Zotero item for completeness issues (missing fields, formatting problems).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The Zotero item key to validate |

**Checks performed:**
- **Universal:** title, creators, date
- **journalArticle:** publicationTitle (issue), volume/pages/DOI (warning)
- **book:** publisher (issue), ISBN (warning)
- **bookSection:** bookTitle (issue), publisher/pages (warning)
- **Formatting:** ALL CAPS title, LaTeX-style `--` page ranges
- **Creators:** missing lastName, missing firstName

**Returns:** Summary header + categorized issues (❌) and warnings (⚠️), or "No issues found."

---

### `get_item_type_fields`

Get the list of valid fields for a Zotero item type.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_type` | `str` | **Yes** | — | Zotero item type (e.g. `journalArticle`, `book`, `bookSection`) |

**Returns:** Formatted list of all fields with their types and defaults. Useful for understanding what fields are available before creating or updating items.

**Note:** Falls back to Web API if local API doesn't serve the `/items/new` template endpoint.

---

## Merging & Audit Tools

### `find_duplicate_candidates`

Scan the Zotero library for potential duplicate items using fuzzy matching.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `min_score` | `float` | No | `0.65` | Minimum similarity score (0–1) to report |
| `limit` | `int` | No | `100` | Maximum items to scan |
| `start` | `int` | No | `0` | Starting offset for pagination |
| `batch_size` | `int` | No | `50` | Items per API call (max 100) |

**Behavior:**
- Fetches items and performs pairwise comparison using title, authors, date, and DOI.
- Returns top 20 duplicate pairs sorted by score.
- Can be slow for large `limit` values (O(n²) comparisons).

---

### `find_duplicates_for_item`

Find potential duplicates of a specific item in the library.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The item key to find duplicates of |
| `min_score` | `float` | No | `0.5` | Minimum similarity score to report |
| `search_limit` | `int` | No | `20` | How many search results to check |

**Behavior:** Searches library by the item's first 5 title words, then scores each candidate. Faster than `find_duplicate_candidates` for checking a single item.

---

### `compare_items_for_merge`

Show a detailed side-by-side comparison of two items for merge decisions.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key_a` | `str` | **Yes** | — | First item key |
| `item_key_b` | `str` | **Yes** | — | Second item key |

**Returns:** Similarity score, full metadata for both items, and a field-by-field diff highlighting which item has more complete data for each field. Recommends using `merge_items` for the actual merge.

---

### `merge_items`

Merge two Zotero items by updating the primary and deleting the secondary.

**⚠️ DESTRUCTIVE OPERATION** — returns a preview by default. Set `confirm=True` to execute.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `primary_key` | `str` | **Yes** | — | Item key to keep (the "winner") |
| `secondary_key` | `str` | **Yes** | — | Item key to merge in and delete (the "loser") |
| `field_overrides` | `dict` | No | `None` | Explicit field values to set on the merged item (highest priority) |
| `use_secondary_fields` | `list[str]` | No | `None` | Field names where the secondary item's value should replace the primary's |
| `merge_tags` | `bool` | No | `True` | Combine tags from both items |
| `confirm` | `bool` | No | `False` | Must be `True` to execute. `False` returns a preview. |

**Merge priority (highest to lowest):**
1. `field_overrides` — explicit values you specify
2. `use_secondary_fields` — pull specific fields from the secondary
3. Primary item's existing values (default)

**Behavior:**
- Preview mode (`confirm=False`): Shows what would change, which fields would be taken from secondary, merged tags, and a deletion warning.
- Execute mode (`confirm=True`): Updates primary with merged data, then deletes secondary via Web API.

---

### `batch_validate_items`

Audit multiple library items for completeness and formatting issues.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | `int` | No | `50` | Number of items to scan |
| `start` | `int` | No | `0` | Starting offset for pagination |
| `item_type` | `str` | No | `None` | Optional filter by item type (e.g. `"journalArticle"`) |

**Returns:** Summary counts (clean / issues / warnings-only) and detailed per-item issue reports. Skips attachment and note items.

---

## File Operations Tools

### `attach_file_linked`

Attach a file to a Zotero item as a **linked file** (Zotero stores only the path reference).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The parent Zotero item key |
| `file_path` | `str` | **Yes** | — | Absolute path to the file on disk |
| `title` | `str` | No | filename | Display title for the attachment |

**Best for:** Existing organized file collections you don't want duplicated. The file stays where it is.

**Behavior:**
- Checks file exists on disk.
- Checks if already linked (prevents duplicate attachments).
- Creates linked_file attachment via Web API.

**Supported file types:** Any — PDF, Markdown, Word, plain text, images, etc.

---

### `attach_file_imported`

Attach a file to a Zotero item as an **imported file** (Zotero copies the file into its cloud storage).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The parent Zotero item key |
| `file_path` | `str` | **Yes** | — | Absolute path to the source file on disk |
| `title` | `str` | No | filename | Display title for the attachment |

**Best for:** Ensuring Zotero has its own copy. Portable libraries. Syncing across devices.

**Behavior:**
1. Creates attachment metadata item via Web API.
2. Uploads file content to Zotero's S3 storage using the multi-step upload protocol.
3. File syncs across all Zotero clients.

---

### `check_item_attachments`

List all attachments (files, links) for a Zotero item.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_key` | `str` | **Yes** | — | The Zotero item key |

**Returns:** For each attachment: key, title, link mode (linked_file / imported_file), content type, path (for linked files with exists check), filename (for imported files).

---

### `scan_directory_for_sources`

Scan a directory of PDF/MD files, extract metadata, and optionally match against the Zotero library.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `directory` | `str` | **Yes** | — | Directory path to scan |
| `extensions` | `list[str]` | No | `[".pdf", ".md"]` | File extensions to include |
| `recursive` | `bool` | No | `True` | Scan subdirectories |
| `max_files` | `int` | No | `500` | Maximum files to process |
| `match_against_library` | `bool` | No | `True` | Whether to search Zotero for matches |
| `match_threshold` | `float` | No | `0.55` | Minimum score to consider a match |

**Metadata extraction:**
- **PDFs:** Attempts DOI extraction from first pages via regex.
- **Markdown:** Extracts from YAML frontmatter (title, authors, date, DOI, ISBN).
- **Filenames:** Parses `Author (Year) Title.ext` patterns.

**Results sorted into three buckets:**
- ✅ **Confident matches** (score ≥ 0.7): likely already in library
- ⚠️ **Uncertain matches** (score ≥ threshold): need human review
- 🆕 **No match**: new items that could be added

---

### `bulk_link_files`

Batch-attach multiple files to their matching Zotero items.

**⚠️ BATCH OPERATION** — returns a preview by default. Set `confirm=True` to execute.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `mappings` | `list[dict]` | **Yes** | — | List of dicts, each with `file_path`, `item_key`, and optional `title` |
| `mode` | `str` | No | `"linked"` | `"linked"` for linked files, `"imported"` for Zotero-managed copies |
| `confirm` | `bool` | No | `False` | Must be `True` to execute |

**Example mappings:**
```json
[
  {"file_path": "/path/to/paper.pdf", "item_key": "BCBLBCMI"},
  {"file_path": "/path/to/book.md", "item_key": "FKX4BMST", "title": "Sanders (1979) Full Text"}
]
```

---

## Safety System

All write operations respect the `SAFETY_MODE` environment variable:

| Mode | `create_item` | `update_item_fields` | `add/remove_tags` | `merge_items` | `bulk_link_files` |
|------|--------------|---------------------|-------------------|--------------|------------------|
| `strict` | Confirm | Confirm | Confirm | Confirm | Confirm |
| `standard` | Auto | Auto | Auto | **Confirm** | **Confirm** |
| `autonomous` | Auto | Auto | Auto | Auto | Auto |

For tools with `confirm` parameters (`merge_items`, `bulk_link_files`), the preview/confirm pattern provides an additional safety layer regardless of safety mode.

---

## Error Handling

- **412 Precondition Failed**: Automatic retry with fresh version number (handles concurrent edits).
- **404 Not Found**: Clear error message identifying the missing item or endpoint.
- **Crossref errors**: Reported as "Could not resolve DOI" with instructions to check validity.
- **File not found**: Reported before any API calls are made.
- **BibTeX parse errors**: Reported with instruction to check formatting.

---

## Companion Read Server: zotero-mcp

The read-only `zotero-mcp` server (by 54yyyu) provides 20 additional tools:

| Tool | Description |
|------|-------------|
| `zotero_search_items` | Search by title/creator/year |
| `zotero_search_by_tag` | Search by tag with AND/OR/NOT |
| `zotero_advanced_search` | Multi-criteria search |
| `zotero_semantic_search` | AI-powered embedding search |
| `zotero_get_item_metadata` | Get metadata (supports BibTeX format) |
| `zotero_get_item_fulltext` | Get full text content |
| `zotero_get_item_children` | Get child items (attachments, notes) |
| `zotero_get_collections` | List all collections |
| `zotero_get_collection_items` | Get items in a collection |
| `zotero_get_tags` | List all tags |
| `zotero_get_recent` | Get recently added items |
| `zotero_get_annotations` | Get annotations |
| `zotero_get_notes` | Get notes |
| `zotero_search_notes` | Search notes |
| `zotero_create_note` | Create a note on an item |
| `zotero_batch_update_tags` | Batch add/remove tags |
| `zotero_update_search_database` | Update semantic search index |
| `zotero_get_search_database_status` | Check index status |
| `search` | ChatGPT-compatible search wrapper |
| `fetch` | ChatGPT-compatible fetch wrapper |
