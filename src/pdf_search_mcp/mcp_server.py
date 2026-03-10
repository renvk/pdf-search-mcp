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
def read_page_image(
    filename: str,
    page: int,
    dpi: int = 140,
    region: list[float] | None = None,
    subfolder: str = "",
) -> str:
    """Render a PDF page as a PNG image for visual inspection.

    Use this instead of read_page when the page contains formulas, diagrams,
    or tables that don't extract well as text. Returns a file path — use the
    Read tool on that path to view the rendered image.

    To zoom into a section (e.g. a formula), pass region=[x1, y1, x2, y2]
    with each value 0.0–1.0 (top-left origin). DPI auto-scales to maximize
    detail for the cropped area. Example: region=[0.0, 0.5, 1.0, 0.8] renders
    the horizontal band from 50% to 80% down the page.

    Args:
        filename: PDF filename exactly as shown in search results.
        page: 1-based page number.
        dpi: Max render resolution (default 140, capped at 600). Auto-scaled
            when region is set.
        region: Optional crop box [x1, y1, x2, y2] with 0.0–1.0 fractional
            coords. (0,0) = top-left, (1,1) = bottom-right.
        subfolder: Subfolder as shown in search results (needed if duplicate
            filenames exist).

    Returns:
        Path to the rendered PNG file.
    """
    dpi = min(dpi, _MAX_DPI)
    if region is not None:
        if len(region) != 4:
            return "region must be [x1, y1, x2, y2] (4 floats, each 0.0–1.0)."
        if not all(0.0 <= v <= 1.0 for v in region):
            return "region values must be between 0.0 and 1.0."
        x1, y1, x2, y2 = region
        if x1 >= x2 or y1 >= y2:
            return "Invalid region: x1 must be < x2 and y1 must be < y2."
    try:
        return str(
            render_pdf_page(
                filename, page, dpi=dpi, subfolder=subfolder or None, region=region
            )
        )
    except PdfSearchError as e:
        return str(e)


@mcp.tool()
def stats() -> str:
    """Show PDF search index statistics (file count, page count, DB size, renderer)."""
    try:
        info = index_stats()
    except PdfSearchError:
        return "No index found. Index PDFs first: PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search index"

    lines = [
        f"Files: {info['total_files']}, "
        f"Pages: {info['total_pages']}, "
        f"Last indexed: {info['last_indexed']}, "
        f"DB size: {info['db_size_mb']} MB, "
        f"Renderer: {info['renderer']}",
    ]
    if info["subfolders"]:
        lines.append("Subfolders:")
        for name, cnt in info["subfolders"].items():
            lines.append(f"  {name or '(root)'}: {cnt} files")
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
