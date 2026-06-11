# pdf-search-mcp

MCP server for full-text search across PDF document collections. Built for AI agents — index once, search instantly from any MCP client.

- **Search entire collections** — pre-indexes all PDFs for instant ranked results with snippets, not one file at a time
- **Fully offline** — no API keys, no cloud services, just SQLite FTS5 and PyMuPDF
- **Page rendering** — render pages as PNG for formulas, diagrams, and tables; crop to a region with auto-DPI scaling for detail shots
- **Dual renderer** — CoreGraphics on macOS (sharper math fonts), PyMuPDF on Linux/Windows
- **German-aware** — automatic expansion of `ß↔ss`, `ä↔ae`, `ö↔oe`, `ü↔ue` so both spellings match
- **stdio or HTTP** — per-client subprocess by default, or a standalone shared server for trusted networks (Dockerfile included)

## Installation

### From PyPI

```bash
pip install pdf-search-mcp
```

### From source

```bash
git clone https://github.com/renvk/pdf-search-mcp.git
cd pdf-search-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires Python 3.10+. On macOS, `pyobjc-framework-Quartz` is installed automatically for native CoreGraphics PDF rendering (sharper formula and math font output). On Linux/Windows, PyMuPDF is used as the renderer.

## Quick Start

### 1. Index your PDFs

```bash
PDF_SEARCH_DIR=/path/to/your/pdfs python -m pdf_search_mcp.pdf_search index
```

### 2. Register with your MCP client

The server runs over stdio. Example for Claude Code:

```bash
# project-scoped (only available in the current directory)
claude mcp add pdf-search -- pdf-search-mcp

# or global (available in all projects)
claude mcp add --scope global pdf-search -- pdf-search-mcp
```

For other MCP clients, add to your MCP config:
```json
{
  "mcpServers": {
    "pdf-search": {
      "command": "pdf-search-mcp"
    }
  }
}
```

### 3. Search

Ask your AI agent to search your PDFs — it will use the `search`, `read_page`, and `read_page_image` tools automatically.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `PDF_SEARCH_DIR` | *(none)* | Path to your PDF directory (required for first index, remembered after) |
| `PDF_SEARCH_DB` | `~/.local/share/pdf-search-mcp/pdf_index.db` | Path to the SQLite database file |

## Self-Hosted Server (HTTP)

The server has two transports:

- **stdio** (default, no flags): each MCP client launches its own server subprocess on the same machine. Use this for single-machine setups.
- **http**: one standalone server shares one indexed PDF collection with multiple clients over a trusted network:

```bash
pdf-search-mcp --transport http --host 0.0.0.0 --port 8000
```

The MCP endpoint is `http://<server>:8000/mcp`. Connecting from Claude Code:

```bash
claude mcp add --transport http --scope user pdf-search http://<server>:8000/mcp
```

> **Security:** the HTTP transport has no authentication or TLS. Run it only on trusted networks (LAN, VPN) and never expose it to the internet. The default `--host 127.0.0.1` keeps it local to the machine; binding `0.0.0.0` is an explicit opt-in.

> **Note:** over HTTP, `read_page_image` returns the rendered PNG as inline MCP image content, so page rendering works for clients on other machines (including clients without filesystem access, such as Claude Desktop). Over stdio it returns a file path for the client to open, as before.

### Docker

The repository includes a Dockerfile for container hosts (home servers, NAS devices). On startup the container runs an incremental index sync against the mounted PDF directory (only new, changed, and deleted files are processed) and then serves on port 8000:

```bash
git clone https://github.com/renvk/pdf-search-mcp.git
cd pdf-search-mcp
docker build -t pdf-search-mcp .
docker run -d --name pdf-search \
  -p 8000:8000 \
  -v /path/to/pdfs:/pdfs:ro \
  -v pdf-index:/data \
  pdf-search-mcp
```

Or with Docker Compose:

```yaml
services:
  pdf-search:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - /path/to/pdfs:/pdfs:ro
      - pdf-index:/data
    restart: unless-stopped

volumes:
  pdf-index:
```

New PDFs in the mounted directory are picked up on container restart, or immediately with:

```bash
docker exec pdf-search python -m pdf_search_mcp.pdf_search index
```

Set `PDF_SEARCH_INDEX_ON_START=0` in the container environment to skip the index sync (e.g. when the index is maintained by an external job).

## CLI Usage

The `pdf_search.py` module doubles as a CLI for indexing and direct search:

```bash
# Build index (first time — PDF_SEARCH_DIR required)
PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search index

# Subsequent syncs (path remembered from first index)
python -m pdf_search_mcp.pdf_search index

# Search from command line
python -m pdf_search_mcp.pdf_search search "query terms"

# Read a specific page
python -m pdf_search_mcp.pdf_search read filename.pdf 5

# Show index statistics
python -m pdf_search_mcp.pdf_search stats

# Rebuild index from scratch (path remembered)
python -m pdf_search_mcp.pdf_search reindex
```

## Search Syntax

Uses [SQLite FTS5](https://www.sqlite.org/fts5.html) query syntax:

| Syntax | Example | Description |
|--------|---------|-------------|
| Terms | `distributed consensus` | Both terms must appear (implicit AND) |
| Phrase | `"garbage collection"` | Exact phrase match |
| OR | `mutex OR semaphore` | Either term |
| NOT | `cache NOT redis` | Exclude term |
| Prefix | `concur*` | Prefix matching |
| NEAR | `NEAR(load balancer, 10)` | Terms within 10 tokens of each other |

**Auto-quoting:** Terms containing any special character (dots, hyphens, commas, slashes, colons, ...) are automatically quoted (e.g., `ISO-27001` becomes `"ISO-27001"`, `1:100` becomes `"1:100"`) because FTS5 treats these as token separators or operators. Query preparation guarantees valid FTS5 syntax — stray quotes are dropped, unbalanced parentheses are repaired, and dangling AND/OR operators are trimmed. The one exception is `NOT` without a left operand (FTS5's `NOT` is binary): it is passed through and returns a clear error, because silently searching the excluded term would invert the query's meaning.

**German expansion:** Umlauts and eszett are automatically expanded to their digraph equivalents and vice versa (`ß↔ss`, `ä↔ae`, `ö↔oe`, `ü↔ue`). Searching for `Größe` also finds `Groesse`, and `Weißbuch` also finds `Weissbuch`. Reverse expansion (`ss`→`ß`) replaces one position at a time. Expansion also applies inside `NEAR()` expressions.

**Auto-relaxation:** When a multi-term query returns no results (all terms must appear on the same page), the search automatically relaxes: first by dropping the term least represented in the corpus (chosen by uncapped match counts), then by OR-ing all terms. A note in the output explains what was actually searched. Structured queries (explicit AND, OR, NOT, NEAR, parentheses) are not relaxed.

## MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `search` | `query`, `limit=10` | Full-text search with ranked results and snippets (limit range 1-50) |
| `read_page` | `filename`, `page`, `subfolder=None` | Read the full text of a specific page |
| `read_page_image` | `filename`, `page`, `dpi=140`, `region=None`, `subfolder=None` | Render a page (or cropped region) as PNG. `region=[x1,y1,x2,y2]` with 0.0–1.0 fractional coords to crop; DPI auto-scales for the cropped area |
| `stats` | *(none)* | Show index statistics (file count, pages, DB size, renderer) |

When the same filename exists in several subfolders, `read_page` and `read_page_image` require the `subfolder` parameter (`""` selects the root folder); an unspecified subfolder returns an error listing the candidates instead of picking one arbitrarily.

## Python API

```python
from pdf_search_mcp import (
    search_with_relaxation, search_pdfs, prepare_query,
    read_pdf_page, render_pdf_page, index_pdfs,
)

# Index PDFs
index_pdfs("/path/to/pdfs")

# Search with the full pipeline (auto-quoting, German expansion,
# relaxation) — same behavior as the MCP search tool and the CLI
results, note = search_with_relaxation("ISO-27001 Anhang", limit=5)
for r in results:
    print(f"{r['subfolder']}/{r['file']} p.{r['page']}: {r['snippet']}")

# Low-level: search_pdfs takes a RAW FTS5 MATCH string (no preparation).
# Run user input through prepare_query first.
results = search_pdfs(prepare_query("garbage collection"), limit=5)

# Read full page text
text = read_pdf_page("document.pdf", 42)

# Render full page as PNG
png_path = render_pdf_page("document.pdf", 42)

# Render cropped region (DPI auto-scales to maximize detail)
png_path = render_pdf_page("document.pdf", 42, region=[0.0, 0.5, 1.0, 0.8])
```

## How It Works

1. **Indexing** incrementally syncs your PDF directory into a SQLite FTS5 virtual table. On first run, all PDFs are indexed. On subsequent runs, only new, changed (by mtime/size), and deleted files are processed, each committed individually so an interrupted run resumes where it stopped. Only page content is searchable — filenames, subfolders, and page numbers are stored as unindexed metadata so query terms cannot match them. Directories starting with `_` are skipped.

> **Upgrading to 0.3.0:** the FTS5 schema changed (metadata columns are no longer searchable). Existing indexes are detected and refused with a clear error — run `python -m pdf_search_mcp.pdf_search reindex` once to rebuild.

2. **Searching** runs FTS5 MATCH queries and re-ranks results by combining BM25 relevance with match density — pages where search terms cluster together score higher than pages with the same terms scattered throughout. The density signal blends term concentration (matches per character) and spatial clustering (how tightly grouped the matches are).

3. **Reading** re-opens the original PDF file on disk (path resolved via the stored `pdf_dir` metadata) for full page text or image rendering. Region crops auto-scale DPI to fill a 1568 px long-edge budget, maximizing detail without producing oversized images.

The database stores the text content only — original PDFs are accessed on disk for `read_page` and `read_page_image`. Rendering uses CoreGraphics on macOS and PyMuPDF elsewhere.

## License

MIT
