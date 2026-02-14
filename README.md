# zotero-write-mcp

MCP server for writing, editing, and merging Zotero library entries. Designed as the **write complement** to the read-only [`zotero-mcp`](https://github.com/54yyyu/zotero-mcp) server — together they give AI agents full CRUD access to your Zotero library.

## Architecture

**Hybrid API client** — reads via the Zotero local API (`http://127.0.0.1:23119`), writes via the Zotero Web API (`https://api.zotero.org`). This avoids the local API's read-only limitation while keeping reads fast and offline-capable.

```
┌──────────────┐     local API (reads)      ┌─────────────┐
│  MCP Client  │◄──────────────────────────► │  Zotero 7+  │
│  (Windsurf,  │     web API (writes)        │  Desktop    │
│   Claude)    │◄──────────────────────────► │  + Cloud    │
└──────────────┘                             └─────────────┘
```

## Requirements

- **Python** ≥ 3.10
- **Zotero 7+** running with local API enabled
- **Zotero Web API key** (generate at https://www.zotero.org/settings/keys)
- **uv** package manager (recommended)

## Installation

```bash
uv tool install /path/to/zotero-write-mcp --force
```

The executable installs to `~/.local/bin/zotero-write-mcp.exe` (Windows) or `~/.local/bin/zotero-write-mcp` (Unix).

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ZOTERO_API_KEY` | **Yes** | — | Zotero Web API key |
| `SAFETY_MODE` | No | `standard` | Safety level: `strict`, `standard`, or `autonomous` |

### Windsurf MCP Config

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "zotero-write": {
    "command": "C:\\Users\\<you>\\.local\\bin\\zotero-write-mcp.exe",
    "env": {
      "ZOTERO_API_KEY": "your-api-key-here",
      "SAFETY_MODE": "standard"
    }
  }
}
```

> ⚠️ **BOM trap**: Never use PowerShell `Set-Content -Encoding UTF8` on `mcp_config.json` — it injects a BOM that breaks Windsurf's JSON parser. Use `[System.IO.File]::WriteAllText()` with `UTF8Encoding($false)` instead.

## Tools (18)

### Creation (3)
| Tool | Description |
|------|-------------|
| `create_item` | Create item from explicit metadata fields |
| `create_item_from_doi` | Resolve DOI via Crossref, create entry with auto-dedup |
| `create_item_from_bibtex` | Parse BibTeX string into Zotero item |

### Editing (5)
| Tool | Description |
|------|-------------|
| `update_item_fields` | Update specific fields on an existing item |
| `add_tags_to_item` | Add tags without removing existing ones |
| `remove_tags_from_item` | Remove specific tags |
| `validate_item` | Check item for completeness/formatting issues |
| `get_item_type_fields` | List valid fields for any Zotero item type |

### Merging & Audit (5)
| Tool | Description |
|------|-------------|
| `find_duplicate_candidates` | Fuzzy-scan library for duplicate pairs |
| `find_duplicates_for_item` | Find duplicates of a specific item |
| `compare_items_for_merge` | Side-by-side field-by-field diff |
| `merge_items` | Merge two items with per-field control (preview + confirm) |
| `batch_validate_items` | Audit sweep for missing fields and formatting issues |

### File Operations (5)
| Tool | Description |
|------|-------------|
| `attach_file_linked` | Link a file on disk to a Zotero item (no copy) |
| `attach_file_imported` | Upload a file copy into Zotero cloud storage |
| `check_item_attachments` | List all attachments for an item |
| `scan_directory_for_sources` | Scan PDF/MD directory, extract metadata, match to library |
| `bulk_link_files` | Batch-attach files to matched items |

## Safety Modes

| Mode | Low-risk ops | Destructive ops | Use case |
|------|-------------|-----------------|----------|
| `strict` | Confirm | Confirm | Maximum caution |
| **`standard`** | Auto-execute | Confirm required | Default — daily use |
| `autonomous` | Auto-execute | Auto-execute | Batch scripting |

Destructive operations: `merge_items` (deletes secondary), `bulk_link_files` (batch writes).

## Companion Server

This server handles **writes**. For **reads/search**, use the companion [`zotero-mcp`](https://github.com/54yyyu/zotero-mcp) server which provides 20 read-only tools including semantic search, full-text retrieval, annotation access, and collection browsing.

Together: **38 Zotero tools** available to any MCP client.

## Project Structure

```
zotero-write-mcp/
├── pyproject.toml
├── README.md
├── API_REFERENCE.md
├── src/
│   └── zotero_write_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       ├── server.py      # FastMCP server + 18 tool definitions
│       ├── client.py       # Hybrid API client (local reads, web writes)
│       ├── fileops.py      # File scanning, metadata extraction
│       ├── safety.py       # Safety mode logic
│       └── utils.py        # DOI resolution, BibTeX parsing, fuzzy matching
└── .windsurf/
    ├── skills/
    │   └── zotero-library-management.md
    └── workflows/
        ├── zotero-create-entry.md
        ├── zotero-edit-entry.md
        ├── zotero-merge-duplicates.md
        ├── zotero-library-audit.md
        ├── zotero-attach-source-files.md
        ├── zotero-corpus-scan-link.md
        └── zotero-search-and-lookup.md
```

## License

MIT
