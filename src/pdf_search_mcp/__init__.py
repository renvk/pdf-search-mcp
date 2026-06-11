"""PDF Search MCP — full-text search across PDF document collections."""

from .pdf_search import (
    PdfSearchError,
    index_pdfs,
    index_stats,
    read_pdf_page,
    reindex_pdfs,
    render_pdf_page,
    search_pdfs,
    search_with_relaxation,
)
from .query import prepare_query

__all__ = [
    "PdfSearchError",
    "index_pdfs",
    "search_pdfs",
    "search_with_relaxation",
    "prepare_query",
    "read_pdf_page",
    "render_pdf_page",
    "index_stats",
    "reindex_pdfs",
]
