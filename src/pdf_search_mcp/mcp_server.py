#!/usr/bin/env python3
"""MCP server exposing PDF search tools.

Wraps the pdf_search module for use by MCP clients.
"""

import re
import sqlite3
import sys

from mcp.server.fastmcp import FastMCP

from .pdf_search import DB_PATH, PdfSearchError, index_stats, read_pdf_page, render_pdf_page, search_pdfs
from .query import prepare_query

mcp = FastMCP("pdf-search-mcp")

_MAX_DPI = 600
# Region crops are clipped, so output stays bounded by _MAX_RENDER_EDGE_PX
# (1568 px). Higher DPI just adds detail within that pixel budget.
_MAX_REGION_DPI = 2500

# FTS5 boolean operators — case-sensitive (FTS5 requires uppercase)
_FTS5_OPERATORS = frozenset({"AND", "OR", "NOT"})

# Detect NEAR() expressions to skip relaxation on structured queries
_NEAR_PAT = re.compile(r"NEAR\s*\(", re.IGNORECASE)


def _extract_terms(query):
    """Split a raw query into droppable terms for relaxation.

    Quoted phrases are kept as single terms (including quotes).
    Returns None if the query contains explicit FTS5 operators (AND, OR,
    NOT) or NEAR() expressions — structured queries should not be relaxed.

    Args:
        query: Raw query string (before prepare_query).

    Returns:
        List of term strings, or None if relaxation should be skipped.
    """
    if _NEAR_PAT.search(query):
        return None
    tokens = re.findall(r'"[^"]*"\*?|\S+', query)
    if any(t in _FTS5_OPERATORS for t in tokens):
        return None
    return tokens or None


def _try_search(query, limit):
    """Prepare and execute a search with FTS5 error recovery.

    Runs the query through prepare_query → search_pdfs.  If the prepared
    form triggers an FTS5 syntax error, retries with the raw query.

    Args:
        query: Raw query string.
        limit: Maximum number of results.

    Returns:
        List of result dicts, or empty list on no match / syntax error.
    """
    prepared = prepare_query(query)
    try:
        return search_pdfs(prepared, limit)
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower():
            try:
                return search_pdfs(query, limit)
            except sqlite3.OperationalError:
                return []
        raise


def _format_results(results):
    """Format result dicts as a numbered list with snippets."""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] {r['subfolder']}/{r['file']} p.{r['page']}\n    {r['snippet']}"
        )
    return "\n\n".join(lines)


def _relax_search(terms, limit):
    """Progressively relax a multi-term AND query until results appear.

    Phase 1 (3+ terms): try dropping each term individually, keep the
    variant that returns the most results.  The dropped term is the one
    least represented in the corpus.

    Phase 2: OR all original terms.  BM25 ranking naturally prioritises
    pages matching more terms.

    Args:
        terms: List of query terms (from _extract_terms).
        limit: Maximum number of results.

    Returns:
        (results, note) tuple.  results is a list of dicts, note is a
        human-readable string explaining what was actually searched.
        Both empty when no relaxation produced results.
    """
    # Phase 1: single-term drops
    if len(terms) >= 3:
        best = []
        best_idx = -1
        for i in range(len(terms)):
            subset = terms[:i] + terms[i + 1:]
            results = _try_search(" ".join(subset), limit)
            if len(results) > len(best):
                best = results
                best_idx = i
        if best:
            kept = terms[:best_idx] + terms[best_idx + 1:]
            return best, f"No matches for full query. Relaxed to: {' '.join(kept)}"

    # Phase 2: OR all terms
    results = _try_search(" OR ".join(terms), limit)
    if results:
        return results, "No matches for full query. Showing pages matching any term."

    return [], ""


@mcp.tool()
def search(query: str, limit: int = 10) -> str:
    """Search indexed PDFs using FTS5 full-text search.

    Supports FTS5 syntax: phrases ("exact phrase"), AND (implicit),
    OR, NOT, prefix (term*), NEAR(term1 term2, 10).

    Terms with dots, hyphens, commas, or slashes are auto-quoted (FTS5 treats
    them as token separators). You can also quote them yourself: "13445-3", "v2.1".

    German ß↔ss variants are expanded automatically.

    When no results match all terms (implicit AND), the query is
    automatically relaxed: first by dropping one term at a time, then
    by OR-ing all terms.  A note at the top explains what was searched.

    Args:
        query: FTS5 search query string.
        limit: Maximum number of results (default 10).

    Returns:
        Formatted search results with file, subfolder, page, and snippet.
    """
    results = _try_search(query, limit)
    if results:
        return _format_results(results)

    # Relax multi-term queries that have no explicit operators
    terms = _extract_terms(query)
    if terms and len(terms) >= 2:
        relaxed, note = _relax_search(terms, limit)
        if relaxed:
            return note + "\n\n" + _format_results(relaxed)

    return "No results found."


@mcp.tool()
def read_page(filename: str, page: int, subfolder: str = "") -> str:
    """Read the full text of a specific page from an indexed PDF.

    Use after search() to read the complete page content around a match.
    If the result contains garbled text, broken symbols, or unreadable
    formulas, use read_page_image() instead — it renders the page as a
    PNG that preserves formulas, diagrams, and tables exactly. For tables
    and dense data, crop to the relevant region to read values reliably.

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
    """Render a PDF page (or cropped region) as a PNG for visual inspection.

    Use instead of read_page() when text extraction misses formulas, diagrams,
    or tables. Returns a file path — read it with the Read tool to view.

    Workflow:
    1. First call: render the full page (no region, default dpi) to orient
       yourself. Do NOT raise dpi — default 140 already fills the vision
       model's 1568 px input limit. Higher DPI just gets downscaled.
    2. ALWAYS crop before reading values. Tables, formulas, and dense data
       are NOT reliably readable at full-page scale. Call again with region
       to crop the area of interest. DPI auto-scales to fill 1568 px for
       the crop — do NOT set dpi manually, it is computed automatically.

    Args:
        filename: PDF filename exactly as shown in search results.
        page: 1-based page number.
        dpi: Render resolution (default 140, capped at 600). Leave at
            default for full-page renders. Ignored when region is set
            (auto-scaled to fill 1568 px).
        region: Crop box [x1, y1, x2, y2], each value 0.0–1.0, top-left
            origin. Required for reading values from tables, formulas, or
            figures — full-page scale is not reliable for these.
            Example: [0.0, 0.5, 1.0, 0.8] = band from 50–80% down the page.
        subfolder: Subfolder as shown in search results.

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
        # Region output is clipped to _MAX_RENDER_EDGE_PX, so higher DPI
        # only adds detail — no risk of oversized images.
        dpi = _MAX_REGION_DPI
    try:
        path = str(
            render_pdf_page(
                filename, page, dpi=dpi, subfolder=subfolder or None, region=region
            )
        )
    except PdfSearchError as e:
        return str(e)
    if region is None:
        path += (
            "\nCrop to tables/figures with region before reading values"
            " (e.g. region=[0.0, 0.3, 1.0, 0.7] for a mid-page table)."
        )
    return path


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
