#!/usr/bin/env python3
"""MCP server exposing PDF search tools.

Wraps the pdf_search module for use by MCP clients.
"""

import sqlite3
import sys

from mcp.server.fastmcp import FastMCP

from .pdf_search import DB_PATH, PdfSearchError, index_stats, read_pdf_page, render_pdf_page, search_pdfs
from .query import prepare_query

mcp = FastMCP("pdf-search-mcp")

_MAX_DPI = 600


@mcp.tool()
def search(query: str, limit: int = 10) -> str:
    """Search indexed PDFs using FTS5 full-text search.

    Supports FTS5 syntax: phrases ("exact phrase"), AND (implicit),
    OR, NOT, prefix (term*), NEAR(term1 term2, 10).

    Terms with dots, hyphens, or commas are auto-quoted (FTS5 treats them as
    token separators). You can also quote them yourself: "13445-3", "v2.1".

    German ß↔ss variants are expanded automatically.

    Args:
        query: FTS5 search query string.
        limit: Maximum number of results (default 10).

    Returns:
        Formatted search results with file, subfolder, page, and snippet.
    """
    prepared = prepare_query(query)
    try:
        results = search_pdfs(prepared, limit)
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower():
            # Fallback: if prepared query has invalid FTS5 syntax,
            # attempt raw query as last resort
            results = search_pdfs(query, limit)
        else:
            raise
    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] {r['subfolder']}/{r['file']} p.{r['page']}\n    {r['snippet']}"
        )
    return "\n\n".join(lines)


@mcp.tool()
def read_page(filename: str, page: int, subfolder: str = "") -> str:
    """Read the full text of a specific page from an indexed PDF.

    Use after search() to read the complete page content around a match.
    If the result contains garbled text, broken symbols, or unreadable
    formulas, use read_page_image() instead — it renders the page as a
    PNG that preserves formulas, diagrams, and tables exactly.

    Args:
        filename: PDF filename exactly as shown in search results.
        page: 1-based page number.
        subfolder: Subfolder as shown in search results (needed if duplicate filenames exist).

    Returns:
        Full extracted text of the page.
    """
    try:
        text = read_pdf_page(filename, page, subfolder=subfolder or None)
        return text if text else "No text found on this page."
    except PdfSearchError as e:
        return str(e)


@mcp.tool()
def read_page_image(filename: str, page: int, dpi: int = 150, subfolder: str = "") -> str:
    """Render a PDF page as an image (PNG) for visual inspection.

    Use this instead of read_page when the page contains formulas, diagrams,
    or tables that don't extract well as text. Returns a file path — use the
    Read tool on that path to view the rendered image.

    For pages with mathematical formulas or equations, use dpi=300 or higher.
    If formulas are still unreadable, retry at 450-600 dpi.

    Args:
        filename: PDF filename exactly as shown in search results.
        page: 1-based page number.
        dpi: Render resolution (default 150, 300+ for formulas/equations, max 600).
        subfolder: Subfolder as shown in search results (needed if duplicate filenames exist).

    Returns:
        Path to the rendered PNG file.
    """
    dpi = min(dpi, _MAX_DPI)
    try:
        return str(render_pdf_page(filename, page, dpi=dpi, subfolder=subfolder or None))
    except PdfSearchError as e:
        return str(e)


@mcp.tool()
def stats() -> str:
    """Show PDF search index statistics (file count, page count, DB size)."""
    try:
        info = index_stats()
    except PdfSearchError:
        return "No index found. Index PDFs first: PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search index"

    lines = [
        f"Files: {info['total_files']}, "
        f"Pages: {info['total_pages']}, "
        f"Last indexed: {info['last_indexed']}, "
        f"DB size: {info['db_size_mb']} MB",
    ]
    if info["subfolders"]:
        lines.append("Subfolders:")
        for name, cnt in info["subfolders"].items():
            lines.append(f"  {name}: {cnt} files")
    return "\n".join(lines)


def main():
    """Entry point for the console script and python -m."""
    if not DB_PATH.exists():
        print(
            "Warning: No search index found. Index PDFs first:",
            "PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search index",
            file=sys.stderr,
        )
    mcp.run()
