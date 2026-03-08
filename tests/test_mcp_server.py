"""Tests for mcp_server.py — thin wrapper over pdf_search functions."""

import sqlite3
from unittest.mock import patch

import pytest

from pdf_search_mcp.mcp_server import read_page, read_page_image, search, stats
from pdf_search_mcp.pdf_search import PdfSearchError


# --- search ---


class TestSearch:
    def test_formatted_output(self, indexed_db):
        """Output should have [1] numbering and p. page references."""
        result = search("pressure")
        assert "[1]" in result
        assert "p." in result

    def test_no_results(self, indexed_db):
        result = search("xyznonexistent")
        assert result == "No results found."

    def test_fts5_error_fallback(self, indexed_db):
        """When prepared query causes OperationalError, falls back to raw query."""
        original_search = __import__("pdf_search_mcp.pdf_search", fromlist=["search_pdfs"]).search_pdfs
        call_count = 0

        def mock_search(query, limit=10):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise sqlite3.OperationalError("fts5: syntax error")
            return original_search(query, limit)

        with patch("pdf_search_mcp.mcp_server.search_pdfs", side_effect=mock_search):
            result = search("pressure")
        # Should have fallen back and returned results
        assert call_count == 2


# --- read_page ---


class TestReadPage:
    def test_returns_text(self, indexed_db):
        result = read_page("basics.pdf", 1)
        assert "pressure vessels" in result

    def test_empty_subfolder_converts_to_none(self, indexed_db):
        """Empty string subfolder should work (converted to None internally)."""
        result = read_page("basics.pdf", 1, subfolder="")
        assert "pressure vessels" in result

    def test_error_returned_as_string(self, indexed_db):
        """PdfSearchError should be returned as string, not raised."""
        result = read_page("nonexistent.pdf", 1)
        assert isinstance(result, str)
        assert "not found" in result.lower()


# --- read_page_image ---


class TestReadPageImage:
    def test_returns_png_path(self, indexed_db):
        result = read_page_image("basics.pdf", 1)
        assert result.endswith(".png")

    def test_dpi_capped_at_max(self, indexed_db):
        """DPI values above _MAX_DPI (600) should be clamped."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            read_page_image("basics.pdf", 1, dpi=1000)
            _, kwargs = mock_render.call_args
            assert kwargs.get("dpi", mock_render.call_args[0][2] if len(mock_render.call_args[0]) > 2 else None) == 600

    def test_dpi_capped_verified(self, indexed_db):
        """Verify DPI capping by checking the actual call args."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            read_page_image("basics.pdf", 1, dpi=1000)
            # render_pdf_page(filename, page, dpi=dpi, subfolder=subfolder or None)
            call_kwargs = mock_render.call_args
            # Check dpi is 600 (capped from 1000)
            assert call_kwargs.kwargs["dpi"] == 600

    def test_error_returned_as_string(self, indexed_db):
        """PdfSearchError should be returned as string, not raised."""
        result = read_page_image("nonexistent.pdf", 1)
        assert isinstance(result, str)
        assert "not found" in result.lower()


# --- stats ---


class TestStats:
    def test_output_contains_fields(self, indexed_db):
        result = stats()
        assert "Files:" in result
        assert "Pages:" in result
        assert "DB size:" in result

    def test_no_index_returns_help(self, temp_db):
        """No index should return help message, not raise."""
        result = stats()
        assert "No index found" in result
