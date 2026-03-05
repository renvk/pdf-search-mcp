# pdf-search-mcp

MCP server for full-text search across PDF document collections. Built for AI agents — index once, search instantly from any MCP client.

- **Search entire collections** — pre-indexes all PDFs for instant ranked results with snippets, not one file at a time
- **Fully offline** — no API keys, no cloud services, just SQLite FTS5 and PyMuPDF
- **Page rendering** — view formulas, diagrams, and tables as PNG when text extraction isn't enough
- **German-aware** — automatic expansion of `ß↔ss`, `ä↔ae`, `ö↔oe`, `ü↔ue` so both spellings match

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

Requires Python 3.10+.

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
| `PDF_SEARCH_DIR` | *(none)* | Path to your PDF directory (required for indexing) |
| `PDF_SEARCH_DB` | `~/.local/share/pdf-search-mcp/pdf_index.db` | Path to the SQLite database file |

## CLI Usage

The `pdf_search.py` module doubles as a CLI for indexing and direct search:

```bash
# Build index
PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search index

# Search from command line
python -m pdf_search_mcp.pdf_search search "query terms"

# Read a specific page
python -m pdf_search_mcp.pdf_search read filename.pdf 5

# Show index statistics
python -m pdf_search_mcp.pdf_search stats

# Rebuild index from scratch
PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search reindex
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

**Auto-quoting:** Terms containing dots, hyphens, or commas are automatically quoted (e.g., `ISO-27001` becomes `"ISO-27001"`) because FTS5 treats these as token separators.

**German expansion:** Umlauts and eszett are automatically expanded to their digraph equivalents and vice versa (`ß↔ss`, `ä↔ae`, `ö↔oe`, `ü↔ue`). Searching for `Größe` also finds `Groesse`, and `Weißbuch` also finds `Weissbuch`.

## MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `search` | `query`, `limit=10` | Full-text search with ranked results and snippets |
| `read_page` | `filename`, `page` | Read the full text of a specific page |
| `read_page_image` | `filename`, `page`, `dpi=150` | Render a page as PNG (for formulas, diagrams, tables) |
| `stats` | *(none)* | Show index statistics |

## Python API

```python
from pdf_search_mcp import search_pdfs, read_pdf_page, render_pdf_page, index_pdfs

# Index PDFs
index_pdfs("/path/to/pdfs")

# Search
results = search_pdfs("garbage collection", limit=5)
for r in results:
    print(f"{r['subfolder']}/{r['file']} p.{r['page']}: {r['snippet']}")

# Read full page text
text = read_pdf_page("document.pdf", 42)

# Render page as PNG
png_path = render_pdf_page("document.pdf", 42, dpi=150)
```

## How It Works

1. **Indexing** walks your PDF directory, extracts text from every page using PyMuPDF, and stores it in a SQLite FTS5 virtual table. Subdirectory names are preserved as a `subfolder` column for context.

2. **Searching** runs FTS5 MATCH queries with relevance ranking (`rank`) and returns snippets showing matching context.

3. **Reading** re-opens the original PDF file on disk (path resolved via the stored `pdf_dir` metadata) for full page text or image rendering.

The database stores the text content only — original PDFs are accessed on disk for `read_page` and `read_page_image`.

## License

MIT
