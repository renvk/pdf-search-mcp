"""Tests for mcp_server.py — thin wrapper over pdf_search functions."""

import sqlite3
from unittest.mock import patch

import pytest

from pdf_search_mcp.mcp_server import (
    _extract_terms,
    _relax_search,
    _try_search,
    read_page,
    read_page_image,
    search,
    stats,
)
from pdf_search_mcp.pdf_search import PdfSearchError


# --- search ---


class TestSearch:
    def test_formatted_output(self, indexed_db):
        """Output should have [1] numbering and p. page references."""
        result = search("pressure")
        assert "[1]" in result
        assert "p." in result

    def test_no_results(self, indexed_db):
        """Single nonexistent term — no relaxation possible."""
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


# --- query relaxation ---


class TestExtractTerms:
    """Unit tests for _extract_terms — decides whether relaxation applies."""

    def test_simple_terms(self):
        assert _extract_terms("pressure vessels piping") == ["pressure", "vessels", "piping"]

    def test_quoted_phrase_kept_whole(self):
        """A quoted phrase counts as one droppable term."""
        assert _extract_terms('"AD 2000" Merkblatt') == ['"AD 2000"', "Merkblatt"]

    def test_prefix_with_quotes(self):
        """Quoted prefix search preserved as single term."""
        assert _extract_terms('"EN-13445"* design') == ['"EN-13445"*', "design"]

    def test_returns_none_for_and(self):
        """Explicit AND means the user structured the query — don't relax."""
        assert _extract_terms("pressure AND vessels") is None

    def test_returns_none_for_or(self):
        assert _extract_terms("pressure OR vessels") is None

    def test_returns_none_for_not(self):
        assert _extract_terms("pressure NOT vessels") is None

    def test_returns_none_for_near(self):
        assert _extract_terms("NEAR(pressure vessels, 5)") is None

    def test_returns_none_for_empty(self):
        assert _extract_terms("") is None

    def test_single_term(self):
        assert _extract_terms("pressure") == ["pressure"]

    def test_case_sensitive_operators(self):
        """Lowercase 'and' is a regular word in FTS5, not an operator."""
        assert _extract_terms("rock and roll") == ["rock", "and", "roll"]


class TestRelaxSearch:
    """Integration tests for _relax_search against the sample index."""

    def test_drops_nonexistent_term(self, indexed_db):
        """'pressure xyznonexistent bogus' — Phase 1 drops one bad term at a time,
        finds that dropping the right term yields the most results."""
        # 'pressure' exists in the index; 'xyznonexistent' and 'bogus' don't.
        # With 3 terms, Phase 1 tries all single drops.  Dropping either
        # nonexistent term still leaves one nonexistent term (AND fails).
        # Phase 2 (OR) should catch it — 'pressure' alone matches.
        results, note = _relax_search(["pressure", "xyznonexistent", "bogus"], 10)
        assert results
        assert "any term" in note or "Relaxed to" in note

    def test_phase1_finds_best_drop(self, indexed_db):
        """'pressure vessels xyznonexistent' — dropping the nonexistent term
        leaves 'pressure vessels' which matches page 1 of basics.pdf."""
        results, note = _relax_search(["pressure", "vessels", "xyznonexistent"], 10)
        assert results
        assert "Relaxed to" in note
        assert "xyznonexistent" not in note  # dropped term excluded from note

    def test_two_terms_skips_phase1(self, indexed_db):
        """With 2 terms, Phase 1 is skipped and Phase 2 (OR) runs directly."""
        results, note = _relax_search(["pressure", "xyznonexistent"], 10)
        assert results
        assert "any term" in note

    def test_all_terms_missing(self, indexed_db):
        """All terms absent from corpus — both phases return nothing."""
        results, note = _relax_search(["qqq", "zzz", "xxx"], 10)
        assert results == []
        assert note == ""

    def test_relaxation_preserves_query_pipeline(self, indexed_db):
        """Sub-queries still go through prepare_query (sanitization, German
        expansion).  'Größe' should match via ß→ss expansion even inside
        a relaxed sub-query."""
        # 'Größe' exists in basics.pdf page 2; 'xyznonexistent' forces relaxation
        results, note = _relax_search(["Größe", "xyznonexistent"], 10)
        assert results
        assert any("basics.pdf" in r["file"] for r in results)


class TestSearchRelaxation:
    """End-to-end tests verifying relaxation through the search() tool."""

    def test_direct_match_no_relaxation(self, indexed_db):
        """When all terms match, no relaxation note appears."""
        result = search("pressure vessels")
        assert "[1]" in result
        assert "Relaxed" not in result
        assert "any term" not in result

    def test_relaxation_note_in_output(self, indexed_db):
        """When relaxation triggers, the note precedes the results."""
        result = search("pressure vessels xyznonexistent")
        assert "[1]" in result
        assert "No matches for full query" in result

    def test_no_relaxation_for_operators(self, indexed_db):
        """Queries with explicit AND/OR/NOT bypass relaxation entirely."""
        result = search("xyznonexistent AND pressure")
        # Should get no results (AND requires both), no relaxation attempted
        assert result == "No results found."

    def test_two_bad_terms_no_results(self, indexed_db):
        """All terms nonexistent — relaxation tried but still no results."""
        result = search("qqq zzz")
        assert result == "No results found."


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
        # Full-page renders append a crop hint after the path
        first_line = result.split("\n")[0]
        assert first_line.endswith(".png")

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

    def test_default_dpi_is_140(self, indexed_db):
        """Default DPI changed from 150 to 140 to match Claude's vision budget."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            read_page_image("basics.pdf", 1)
            assert mock_render.call_args.kwargs["dpi"] == 140

    def test_region_returns_png_path(self, indexed_db):
        """Cropped region render should return a valid PNG path."""
        result = read_page_image("basics.pdf", 1, region=[0.0, 0.0, 0.5, 0.5])
        assert result.endswith(".png")

    def test_region_passed_to_render(self, indexed_db):
        """Region parameter should be forwarded to render_pdf_page."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            read_page_image("basics.pdf", 1, region=[0.1, 0.2, 0.8, 0.9])
            assert mock_render.call_args.kwargs["region"] == [0.1, 0.2, 0.8, 0.9]

    def test_region_none_by_default(self, indexed_db):
        """Without region param, None should be passed to render_pdf_page."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            read_page_image("basics.pdf", 1)
            assert mock_render.call_args.kwargs["region"] is None

    def test_region_validation_length(self, indexed_db):
        """Region with wrong number of elements returns error string."""
        result = read_page_image("basics.pdf", 1, region=[0.0, 0.0, 0.5])
        assert "4 floats" in result

    def test_region_validation_bounds(self, indexed_db):
        """Region values outside 0.0–1.0 return error string."""
        result = read_page_image("basics.pdf", 1, region=[0.0, 0.0, 0.5, 1.5])
        assert "0.0 and 1.0" in result

    def test_region_validation_ordering(self, indexed_db):
        """x1 >= x2 or y1 >= y2 returns error string."""
        result = read_page_image("basics.pdf", 1, region=[0.5, 0.0, 0.3, 1.0])
        assert "x1 must be < x2" in result


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
