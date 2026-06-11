#!/usr/bin/env python3
"""MCP server exposing PDF search tools.

Thin wrapper over the pdf_search module: tools validate/clamp tool-level
parameters, run the (blocking) core functions on a worker thread so the
asyncio event loop stays responsive, and convert PdfSearchError into
plain-text replies instead of protocol errors.

Transports: stdio (default, one subprocess per client) and streamable
HTTP (`--transport http`, standalone server for clients on a trusted
network). search, read_page, and stats behave identically on both.
Exception: read_page_image returns a file path on the machine running
the server — usable over stdio (client and server share a filesystem),
not usable by HTTP clients on other machines.

The HTTP transport has no authentication. Bind it only to interfaces
on trusted networks (default bind: 127.0.0.1).

Invariant: query preparation, relaxation, and input validation live in
pdf_search/query — this layer adds nothing the CLI or Python API would
have to duplicate.
"""

import argparse
import sys
from functools import partial

import anyio
from mcp.server.fastmcp import FastMCP

from .pdf_search import (
    DB_PATH,
    PdfSearchError,
    index_stats,
    read_pdf_page,
    render_pdf_page,
    search_with_relaxation,
)

mcp = FastMCP("pdf-search-mcp")

# Upper bound for full-page render DPI. Region crops auto-scale their DPI
# in the core layer instead (see pdf_search._MAX_REGION_DPI).
_MAX_DPI = 600

# Upper bound for search result count — protects the client's context
# window from a runaway limit argument.
_MAX_LIMIT = 50


async def _in_thread(func, *args, **kwargs):
    """Run a blocking core function on a worker thread.

    sqlite queries, PDF parsing, and rendering block for up to seconds;
    running them inline would stall MCP pings and cancellation.
    """
    return await anyio.to_thread.run_sync(partial(func, *args, **kwargs))


def _format_results(results):
    """Format result dicts as a numbered list with snippets."""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] {r['subfolder']}/{r['file']} p.{r['page']}\n    {r['snippet']}"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def search(query: str, limit: int = 10) -> str:
    """Search indexed PDFs using FTS5 full-text search.

    Supports FTS5 syntax: phrases ("exact phrase"), AND (implicit),
    OR, NOT, prefix (term*), NEAR(term1 term2, 10), parentheses.

    Terms with special characters (dots, hyphens, colons, slashes, ...)
    are auto-quoted — FTS5 treats them as token separators. You can also
    quote them yourself: "13445-3", "v2.1".

    German ß↔ss / ä↔ae / ö↔oe / ü↔ue variants are expanded automatically.

    When no results match all terms (implicit AND), the query is
    automatically relaxed: first by dropping the term least represented
    in the corpus, then by OR-ing all terms.  A note at the top explains
    what was searched.  Structured queries (explicit operators, NEAR,
    parentheses) are never relaxed.

    Args:
        query: Search query string.
        limit: Maximum number of results (default 10, range 1-50).

    Returns:
        Formatted search results with file, subfolder, page, and snippet.
    """
    if not query.strip():
        return "Empty query. Provide one or more search terms."
    if limit < 1:
        return "limit must be a positive integer."
    limit = min(limit, _MAX_LIMIT)

    try:
        results, note = await _in_thread(search_with_relaxation, query, limit)
    except PdfSearchError as e:
        return str(e)

    if not results:
        return "No results found."
    if note:
        return note + "\n\n" + _format_results(results)
    return _format_results(results)


@mcp.tool()
async def read_page(filename: str, page: int, subfolder: str | None = None) -> str:
    """Read the full text of a specific page from an indexed PDF.

    Use after search() to read the complete page content around a match.
    If the result contains garbled text, broken symbols, or unreadable
    formulas, use read_page_image() instead — it renders the page as a
    PNG that preserves formulas, diagrams, and tables exactly. For tables
    and dense data, crop to the relevant region to read values reliably.

    Args:
        filename: PDF filename exactly as shown in search results.
        page: 1-based page number.
        subfolder: Subfolder as shown in search results. Required when
            duplicate filenames exist; pass "" for the root folder.

    Returns:
        Full extracted text of the page.
    """
    try:
        text = await _in_thread(read_pdf_page, filename, page, subfolder=subfolder)
        return text if text else "No text found on this page."
    except PdfSearchError as e:
        return str(e)


@mcp.tool()
async def read_page_image(
    filename: str,
    page: int,
    dpi: int = 140,
    region: list[float] | None = None,
    subfolder: str | None = None,
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
        dpi: Render resolution (default 140, range 1-600). Leave at
            default for full-page renders. Ignored when region is set
            (auto-scaled to fill 1568 px).
        region: Crop box [x1, y1, x2, y2], each value 0.0–1.0, top-left
            origin. Required for reading values from tables, formulas, or
            figures — full-page scale is not reliable for these.
            Example: [0.0, 0.5, 1.0, 0.8] = band from 50–80% down the page.
        subfolder: Subfolder as shown in search results. Required when
            duplicate filenames exist; pass "" for the root folder.

    Returns:
        The PNG file path on the first line. Full-page renders append a
        crop-advisory line after the path — when passing the result to a
        file reader, use only the first line.
    """
    dpi = min(dpi, _MAX_DPI)
    try:
        path = str(
            await _in_thread(
                render_pdf_page,
                filename,
                page,
                dpi=dpi,
                subfolder=subfolder,
                region=region,
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
async def stats() -> str:
    """Show PDF search index statistics (file count, page count, DB size, renderer)."""
    try:
        info = await _in_thread(index_stats)
    except PdfSearchError as e:
        return (
            f"{e}\nIndex PDFs first: "
            "PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search index"
        )

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


def _parse_args(argv=None):
    """Parse server command-line arguments.

    Args:
        argv: list[str] | None — argument vector excluding the program
            name. None reads sys.argv[1:] (the argparse default); tests
            pass an explicit list.

    Returns:
        argparse.Namespace with:
            transport: "stdio" | "http"
            host: str — bind interface, used only when transport="http"
            port: int — bind port (1-65535), used only when transport="http"

    Exits with status 2 (argparse convention) on unknown arguments,
    invalid choices, or a port outside 1-65535.
    """
    parser = argparse.ArgumentParser(
        prog="pdf-search-mcp",
        description="MCP server for full-text search across PDF collections.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio: one server subprocess per client, spawned by the MCP"
        " client (default). http: standalone streamable-HTTP server that"
        " multiple clients connect to over the network.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind interface for --transport http (default: 127.0.0.1;"
        " use 0.0.0.0 to accept connections from other machines)."
        " Ignored for stdio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for --transport http (default: 8000)."
        " Ignored for stdio.",
    )
    args = parser.parse_args(argv)
    # argparse type=int accepts any integer; uvicorn would fail later
    # with an OS-level error, so reject out-of-range ports up front.
    if not 1 <= args.port <= 65535:
        parser.error(f"--port must be in 1-65535, got {args.port}")
    return args


def main():
    """Entry point for the console script and python -m."""
    args = _parse_args()
    if not DB_PATH.exists():
        print(
            "Warning: No search index found. Index PDFs first:",
            "PDF_SEARCH_DIR=/path/to/pdfs python -m pdf_search_mcp.pdf_search index",
            file=sys.stderr,
        )
    if args.transport == "http":
        # FastMCP reads bind address from its settings object, not from
        # run() arguments. The MCP endpoint is served at /mcp (SDK
        # default) — clients connect to http://<host>:<port>/mcp.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
