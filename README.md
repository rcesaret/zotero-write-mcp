# zotero-write-mcp

[![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP](https://img.shields.io/badge/MCP-FastMCP-green.svg)](https://github.com/jlowin/fastmcp)

**Model Context Protocol (MCP) server for writing, editing, and merging Zotero library entries.** Designed as the **write complement** to the read-only [`zotero-mcp`](https://github.com/54yyyu/zotero-mcp) server — together they give AI agents (Claude, Windsurf, Cline, etc.) full CRUD access to your Zotero library.

## ✨ Key Features

- **18 Powerful Tools** — Complete CRUD operations for Zotero items, tags, attachments, and metadata
- **Hybrid API Architecture** — Fast local reads, reliable cloud writes
- **Smart Duplicate Detection** — Fuzzy matching and merge capabilities
- **DOI & BibTeX Support** — Create entries from DOIs or BibTeX strings
- **Bulk File Operations** — Scan directories, extract metadata, batch-attach PDFs
- **Configurable Safety Modes** — Strict, standard, or autonomous operation
- **Zero-Config Local Setup** — Works with Zotero 7+ local API (no additional setup needed)

## 📋 Table of Contents

- [Key Features](#-key-features)
- [Architecture](#-architecture)
- [Requirements](#-requirements)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Quick Start](#-quick-start)
- [Tools Overview](#-tools-overview-18)
- [Safety Modes](#-safety-modes)
- [Usage Examples](#-usage-examples)
- [Companion Server](#-companion-server)
- [Technical Specifications](#-technical-specifications)
- [Troubleshooting](#-troubleshooting)
- [Contributing](#-contributing)
- [License](#-license)

## 🏗 Architecture

**Hybrid API client** — reads via the Zotero local API (`http://127.0.0.1:23119`), writes via the Zotero Web API (`https://api.zotero.org`). This avoids the local API's read-only limitation while keeping reads fast and offline-capable.

```
┌──────────────┐     local API (reads)      ┌─────────────┐
│  MCP Client  │◄──────────────────────────► │  Zotero 7+  │
│  (Windsurf,  │     web API (writes)        │  Desktop    │
│   Claude)    │◄──────────────────────────► │  + Cloud    │
└──────────────┘                             └─────────────┘
```

### Why Hybrid?

- **Fast Reads**: Local API responds instantly, works offline
- **Reliable Writes**: Web API ensures data consistency across devices
- **No Sync Delays**: Changes propagate through Zotero's sync system
- **Full Feature Access**: Web API supports all write operations

## 📦 Requirements

- **Python** ≥ 3.10
- **Zotero 7+** running with local API enabled (automatically enabled in Zotero 7+)
- **Zotero Web API key** — Generate at https://www.zotero.org/settings/keys
  - Required permissions: **Read and Write** access to your library
  - User ID will be auto-detected from API key
- **uv** package manager (recommended) or pip

## 🚀 Installation

### Method 1: Using uv (Recommended)

```bash
# Install from local directory
uv tool install /path/to/zotero-write-mcp --force

# Or install from GitHub
uv tool install git+https://github.com/rcesaret/zotero-write-mcp.git
```

The executable installs to:
- **Unix/macOS**: `~/.local/bin/zotero-write-mcp`
- **Windows**: `%USERPROFILE%\.local\bin\zotero-write-mcp.exe`

### Method 2: Using pip

```bash
# Install from local directory
pip install /path/to/zotero-write-mcp

# Or install from GitHub
pip install git+https://github.com/rcesaret/zotero-write-mcp.git
```

### Method 3: From Source

```bash
git clone https://github.com/rcesaret/zotero-write-mcp.git
cd zotero-write-mcp
pip install -e .
```

### Verify Installation

```bash
zotero-write-mcp --version
# Or test the server starts correctly:
zotero-write-mcp
# Press Ctrl+C to exit
```

## ⚙️ Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ZOTERO_API_KEY` | **Yes** | — | Zotero Web API key (generate at https://www.zotero.org/settings/keys) |
| `SAFETY_MODE` | No | `standard` | Safety level: `strict`, `standard`, or `autonomous` |
| `ZOTERO_LOCAL_URL` | No | `http://127.0.0.1:23119/api` | Zotero local API base URL |

### MCP Client Configuration

#### Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "zotero-write": {
      "command": "zotero-write-mcp",
      "env": {
        "ZOTERO_API_KEY": "your-api-key-here",
        "SAFETY_MODE": "standard"
      }
    }
  }
}
```

**Windows users**: Use full path like `"C:\\Users\\<you>\\.local\\bin\\zotero-write-mcp.exe"`

> ⚠️ **BOM Warning**: Never use PowerShell `Set-Content -Encoding UTF8` on `mcp_config.json` — it injects a BOM that breaks Windsurf's JSON parser. Use `[System.IO.File]::WriteAllText()` with `UTF8Encoding($false)` instead.

#### Claude Desktop

Add to:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "zotero-write": {
      "command": "zotero-write-mcp",
      "env": {
        "ZOTERO_API_KEY": "your-api-key-here",
        "SAFETY_MODE": "standard"
      }
    }
  }
}
```

#### Cline (VS Code Extension)

Add to Cline's MCP settings:

```json
{
  "mcpServers": {
    "zotero-write": {
      "command": "zotero-write-mcp",
      "env": {
        "ZOTERO_API_KEY": "your-api-key-here",
        "SAFETY_MODE": "standard"
      }
    }
  }
}
```

#### Other MCP Clients

Any MCP-compatible client can use this server. The server uses stdio transport and follows the [MCP specification](https://modelcontextprotocol.io/).

## 🚦 Quick Start

1. **Get Your Zotero API Key**
   - Visit https://www.zotero.org/settings/keys
   - Click "Create new private key"
   - Give it a name (e.g., "MCP Server")
   - Enable "Allow library access" with **Read/Write** permissions
   - Copy the generated key

2. **Ensure Zotero is Running**
   - Launch Zotero 7+ desktop application
   - The local API runs automatically on `http://127.0.0.1:23119`

3. **Configure Your MCP Client**
   - Add the server to your MCP client's configuration (see examples above)
   - Set your `ZOTERO_API_KEY` in the environment variables

4. **Test the Connection**
   - Ask your AI assistant: "List the tools available for Zotero"
   - Try creating a test item: "Create a test journal article in my Zotero library"

5. **Explore the Tools**
   - See the [Tools Overview](#-tools-overview-18) section below
   - Check [API_REFERENCE.md](API_REFERENCE.md) for detailed parameter documentation

## 🔧 Tools Overview (18)

All tools are fully documented in [API_REFERENCE.md](API_REFERENCE.md) with parameter details and examples.

### Creation (3)

Create new Zotero items from various sources.

| Tool | Description |
|------|-------------|
| `create_item` | Create item from explicit metadata fields |
| `create_item_from_doi` | Resolve DOI via Crossref, create entry with auto-dedup |
| `create_item_from_bibtex` | Parse BibTeX string into Zotero item |

### Editing (5)

Modify existing items and their metadata.

| Tool | Description |
|------|-------------|
| `update_item_fields` | Update specific fields on an existing item |
| `add_tags_to_item` | Add tags without removing existing ones |
| `remove_tags_from_item` | Remove specific tags |
| `validate_item` | Check item for completeness/formatting issues |
| `get_item_type_fields` | List valid fields for any Zotero item type |

### Merging & Audit (5)

Find duplicates, compare items, and perform library audits.

| Tool | Description |
|------|-------------|
| `find_duplicate_candidates` | Fuzzy-scan library for duplicate pairs |
| `find_duplicates_for_item` | Find duplicates of a specific item |
| `compare_items_for_merge` | Side-by-side field-by-field diff |
| `merge_items` | Merge two items with per-field control (preview + confirm) |
| `batch_validate_items` | Audit sweep for missing fields and formatting issues |

### File Operations (5)

Manage attachments and bulk-link files to library items.

| Tool | Description |
|------|-------------|
| `attach_file_linked` | Link a file on disk to a Zotero item (no copy) |
| `attach_file_imported` | Upload a file copy into Zotero cloud storage |
| `check_item_attachments` | List all attachments for an item |
| `scan_directory_for_sources` | Scan PDF/MD directory, extract metadata, match to library |
| `bulk_link_files` | Batch-attach files to matched items |

## 🛡️ Safety Modes

Configure how the server handles potentially destructive operations:

| Mode | Low-risk ops | Destructive ops | Use case |
|------|-------------|-----------------|----------|
| `strict` | Confirm | Confirm | Maximum caution — confirm everything |
| **`standard`** | Auto-execute | Confirm required | **Default** — daily use, confirm destructive ops |
| `autonomous` | Auto-execute | Auto-execute | Batch scripting — no confirmations |

**Destructive operations**: `merge_items` (deletes secondary item), `bulk_link_files` (batch writes)

Set via `SAFETY_MODE` environment variable or prompt your AI assistant to "switch to strict safety mode" during the session.

## 💡 Usage Examples

### Creating Items from DOI

```
You: Add the paper with DOI 10.1038/nature12373 to my library
```

The server will:
1. Fetch metadata from Crossref
2. Check for duplicates
3. Create the item if no duplicate exists
4. Return the new item key

### Bulk Linking PDFs

```
You: Scan ~/Papers/2024 for PDFs and link them to matching items in my library
```

The server will:
1. Extract metadata from PDFs (title, authors, DOI if present)
2. Find matching items in your library
3. Present matches for confirmation (in `standard` mode)
4. Link files to confirmed matches

### Finding and Merging Duplicates

```
You: Find duplicate items in my library and help me merge them
```

The server will:
1. Scan library for potential duplicates
2. Show side-by-side comparisons
3. Let you choose which fields to keep
4. Merge items after confirmation

### Batch Validation

```
You: Check all my conference papers for missing fields
```

The server will:
1. Filter items by type (conferencePaper)
2. Check for common missing fields (venue, date, pages)
3. Report issues with item keys for easy fixing

## 🤝 Companion Server

This server handles **writes**. For **reads/search**, use the companion [`zotero-mcp`](https://github.com/54yyyu/zotero-mcp) server which provides 20 read-only tools including:

- 🔍 Semantic search across your library
- 📄 Full-text retrieval from PDFs
- 📝 Annotation access and filtering
- 📚 Collection browsing and management
- 🔗 Citation network exploration

**Together: 38 Zotero tools** available to any MCP client — complete CRUD operations for your research library.

## 📐 Technical Specifications

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `fastmcp` | ≥2.0 | MCP server framework |
| `httpx` | ≥0.27 | Async HTTP client for API calls |
| `bibtexparser` | ≥1.4,<2.0 | BibTeX parsing and conversion |

### API Endpoints

- **Local API**: `http://127.0.0.1:23119/api` (read operations)
- **Web API**: `https://api.zotero.org/` (write operations)

### Rate Limiting

The server respects Zotero Web API rate limits:
- User library: 120 requests/minute
- Group library: 120 requests/minute
- Automatic retry with backoff on 429 responses

### Data Flow

```
MCP Request → Tool Handler → Safety Check → API Client
                                              ├─► Local API (read)
                                              └─► Web API (write)
                                              
Response ← Format Result ← Parse Response ← API Client
```

### File Structure

```
zotero-write-mcp/
├── pyproject.toml          # Package configuration
├── README.md               # This file
├── API_REFERENCE.md        # Detailed tool documentation
└── src/
    └── zotero_write_mcp/
        ├── __init__.py     # Package initialization
        ├── __main__.py     # Entry point (FastMCP stdio transport)
        ├── server.py       # FastMCP server + 18 tool definitions
        ├── client.py       # Hybrid API client (local reads, web writes)
        ├── fileops.py      # File scanning, PDF metadata extraction
        ├── safety.py       # Safety mode logic and confirmation handling
        └── utils.py        # DOI resolution, BibTeX parsing, fuzzy matching
```

### Supported Item Types

All Zotero item types are supported:
- Articles: `journalArticle`, `magazineArticle`, `newspaperArticle`
- Books: `book`, `bookSection`
- Academic: `thesis`, `conferencePaper`, `report`
- Media: `film`, `tvBroadcast`, `podcast`, `videoRecording`, `audioRecording`
- Legal: `case`, `statute`, `patent`
- Web: `webpage`, `blogPost`, `forumPost`
- And many more — see `get_item_type_fields` tool

## 🐛 Troubleshooting

### Server Won't Start

**Problem**: `zotero-write-mcp` command not found  
**Solution**: Ensure the installation directory is in your PATH
```bash
# Unix/macOS
export PATH="$HOME/.local/bin:$PATH"

# Windows (PowerShell)
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

### API Key Issues

**Problem**: "Invalid API key" or "Authentication failed"  
**Solution**: 
- Verify your API key at https://www.zotero.org/settings/keys
- Ensure it has **Read/Write** permissions
- Check that `ZOTERO_API_KEY` is set correctly in your MCP client config
- No quotes needed in JSON config — use `"ZOTERO_API_KEY": "abcd1234"`

### Local API Connection Failed

**Problem**: "Cannot connect to local API"  
**Solution**:
- Ensure Zotero 7+ desktop application is running
- Check that local API is enabled (it's on by default in Zotero 7+)
- Verify nothing else is using port 23119
- Try accessing `http://127.0.0.1:23119/api` in your browser

### Rate Limiting

**Problem**: "Too many requests" or 429 errors  
**Solution**:
- The server automatically retries with backoff
- Reduce batch operation sizes
- Wait a minute before retrying failed operations

### BOM Issues in JSON Config (Windows)

**Problem**: MCP client fails to parse config file  
**Solution**: Don't use PowerShell's `Set-Content` with UTF8 encoding
```powershell
# Wrong (adds BOM):
Get-Content config.json | Set-Content -Encoding UTF8 config.json

# Right (no BOM):
$json = Get-Content config.json -Raw
[System.IO.File]::WriteAllText("config.json", $json, [System.Text.UTF8Encoding]::new($false))
```

### Debugging

Enable verbose logging in your MCP client to see detailed error messages:

**Windsurf**: Check the output panel for MCP server logs  
**Claude Desktop**: Check logs at:
- macOS: `~/Library/Logs/Claude/`
- Windows: `%APPDATA%\Claude\logs\`

## 🤝 Contributing

Contributions are welcome! This project is being prepared for public release.

### Development Setup

```bash
# Clone the repository
git clone https://github.com/rcesaret/zotero-write-mcp.git
cd zotero-write-mcp

# Install in development mode
pip install -e .

# Test the server
zotero-write-mcp
```

### Guidelines

- Follow the existing code style
- Add tests for new features
- Update API_REFERENCE.md for new tools
- Keep the README up to date

### Reporting Issues

Please report issues at: https://github.com/rcesaret/zotero-write-mcp/issues

Include:
- Your Python version (`python --version`)
- Your Zotero version
- MCP client you're using (Windsurf, Claude, etc.)
- Error messages or unexpected behavior
- Steps to reproduce

## 📚 Additional Resources

- [API Reference](API_REFERENCE.md) — Detailed documentation for all 18 tools
- [MCP Specification](https://modelcontextprotocol.io/) — Learn about the Model Context Protocol
- [Zotero Web API Docs](https://www.zotero.org/support/dev/web_api/v3/start) — Zotero API reference
- [FastMCP Framework](https://github.com/jlowin/fastmcp) — The underlying MCP framework

## 📄 License

MIT
