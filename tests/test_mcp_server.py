"""Tests for mcp_server.py — thin async wrapper over pdf_search functions.

Tools are async (they offload blocking work to a thread so the MCP event
loop stays responsive); tests drive them with asyncio.run().
"""

import asyncio
from unittest.mock import patch

import pytest

from pdf_search_mcp.mcp_server import (
    _parse_args,
    main,
    read_page,
    read_page_image,
    search,
    stats,
)


def run(coro):
    """Drive an async tool to completion from sync test code."""
    return asyncio.run(coro)


# --- search ---


class TestSearch:
    def test_formatted_output(self, indexed_db):
        """Output should have [1] numbering and p. page references."""
        result = run(search("pressure"))
        assert "[1]" in result
        assert "p." in result

    def test_no_results(self, indexed_db):
        """Single nonexistent term — no relaxation possible."""
        result = run(search("xyznonexistent"))
        assert result == "No results found."

    def test_invalid_query_returns_message_not_exception(self, indexed_db):
        """'NOT term' is invalid FTS5 — the tool must return the error as
        text instead of leaking an exception through the MCP layer (and
        must NOT mask it as 'No results found.')."""
        result = run(search("NOT pressure"))
        assert isinstance(result, str)
        assert result != "No results found."
        assert "Search failed" in result

    def test_no_index_returns_message(self, temp_db):
        """Missing index surfaces as guidance text, like the other tools,
        not as a raised PdfSearchError."""
        result = run(search("anything"))
        assert "No index found" in result

    def test_empty_query_returns_message(self, indexed_db):
        result = run(search("   "))
        assert "Empty query" in result

    def test_negative_limit_returns_message(self, indexed_db):
        """limit=-1 previously fetched the entire corpus unbounded and then
        silently dropped the last result via rows[:limit]."""
        result = run(search("pressure", limit=-1))
        assert "positive integer" in result

    def test_excessive_limit_clamped(self, indexed_db):
        """A huge limit is clamped, not an error — the query still runs."""
        result = run(search("pressure", limit=10_000))
        assert "[1]" in result


class TestSearchRelaxation:
    """End-to-end tests verifying relaxation through the search() tool."""

    def test_direct_match_no_relaxation(self, indexed_db):
        """When all terms match, no relaxation note appears."""
        result = run(search("pressure vessels"))
        assert "[1]" in result
        assert "Relaxed" not in result
        assert "any term" not in result

    def test_relaxation_note_in_output(self, indexed_db):
        """When relaxation triggers, the note precedes the results."""
        result = run(search("pressure vessels xyznonexistent"))
        assert "[1]" in result
        assert "No matches for full query" in result

    def test_no_relaxation_for_operators(self, indexed_db):
        """Queries with explicit AND/OR/NOT bypass relaxation entirely."""
        result = run(search("xyznonexistent AND pressure"))
        # Should get no results (AND requires both), no relaxation attempted
        assert result == "No results found."

    def test_two_bad_terms_no_results(self, indexed_db):
        """All terms nonexistent — relaxation tried but still no results."""
        result = run(search("qqq zzz"))
        assert result == "No results found."

    def test_unsearchable_term_degrades_to_no_results(self, indexed_db):
        """PR-review issue 1 end-to-end: 'xyznonexistent *' previously
        returned 'Search failed for query ... OR: fts5: syntax error'
        quoting a query the user never typed."""
        result = run(search("xyznonexistent *"))
        assert result == "No results found."

    def test_german_expansion_through_tool(self, indexed_db):
        """'Aussendurchmesser' must find the page containing
        'Außendurchmesser' (reverse expansion replaces one digraph
        position at a time, so a single-conversion word is used)."""
        result = run(search("Aussendurchmesser"))
        assert "basics.pdf" in result


# --- read_page ---


class TestReadPage:
    def test_returns_text(self, indexed_db):
        result = run(read_page("basics.pdf", 1))
        assert "pressure vessels" in result

    def test_empty_subfolder_selects_root(self, indexed_db):
        """subfolder='' is the explicit root-folder selector and must reach
        the core layer unchanged — previously `subfolder or None` coerced
        it to 'unspecified', so root copies of duplicates were unselectable."""
        result = run(read_page("basics.pdf", 1, subfolder=""))
        assert "pressure vessels" in result

    def test_duplicate_filename_returns_guidance(self, temp_db, sample_pdfs, make_pdf):
        """Ambiguous duplicates return an instructive error string instead
        of silently reading an arbitrary copy."""
        from pdf_search_mcp.pdf_search import index_pdfs

        make_pdf(sample_pdfs / "dup.pdf", "ROOT copy")
        sub = sample_pdfs / "sub"
        sub.mkdir()
        make_pdf(sub / "dup.pdf", "SUB copy")
        index_pdfs(str(sample_pdfs))

        result = run(read_page("dup.pdf", 1))
        assert "Multiple files named" in result

    def test_error_returned_as_string(self, indexed_db):
        """PdfSearchError should be returned as string, not raised."""
        result = run(read_page("nonexistent.pdf", 1))
        assert isinstance(result, str)
        assert "not found" in result.lower()


# --- read_page_image ---


class TestReadPageImage:
    def test_returns_png_path(self, indexed_db):
        result = run(read_page_image("basics.pdf", 1))
        # Full-page renders append a crop hint after the path
        first_line = result.split("\n")[0]
        assert first_line.endswith(".png")

    def test_dpi_capped_at_max(self, indexed_db):
        """DPI values above _MAX_DPI (600) should be clamped."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            run(read_page_image("basics.pdf", 1, dpi=1000))
            assert mock_render.call_args.kwargs["dpi"] == 600

    def test_error_returned_as_string(self, indexed_db):
        """PdfSearchError should be returned as string, not raised."""
        result = run(read_page_image("nonexistent.pdf", 1))
        assert isinstance(result, str)
        assert "not found" in result.lower()

    def test_zero_dpi_returns_message(self, indexed_db):
        """dpi=0 previously produced a 0-byte PNG reported as success."""
        result = run(read_page_image("basics.pdf", 1, dpi=0))
        assert "dpi must be" in result

    def test_default_dpi_is_140(self, indexed_db):
        """Default DPI matches Claude's vision budget (1568 px long edge)."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            run(read_page_image("basics.pdf", 1))
            assert mock_render.call_args.kwargs["dpi"] == 140

    def test_region_returns_png_path(self, indexed_db):
        """Cropped region render should return a valid PNG path."""
        result = run(read_page_image("basics.pdf", 1, region=[0.0, 0.0, 0.5, 0.5]))
        assert result.endswith(".png")

    def test_region_passed_to_render(self, indexed_db):
        """Region parameter should be forwarded to render_pdf_page."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            run(read_page_image("basics.pdf", 1, region=[0.1, 0.2, 0.8, 0.9]))
            assert mock_render.call_args.kwargs["region"] == [0.1, 0.2, 0.8, 0.9]

    def test_region_none_by_default(self, indexed_db):
        """Without region param, None should be passed to render_pdf_page."""
        with patch("pdf_search_mcp.mcp_server.render_pdf_page") as mock_render:
            mock_render.return_value = "/tmp/test.png"
            run(read_page_image("basics.pdf", 1))
            assert mock_render.call_args.kwargs["region"] is None

    def test_region_validation_length(self, indexed_db):
        """Region with wrong number of elements returns error string
        (validation lives in the core layer, surfaced as text here)."""
        result = run(read_page_image("basics.pdf", 1, region=[0.0, 0.0, 0.5]))
        assert "4 floats" in result

    def test_region_validation_bounds(self, indexed_db):
        """Region values outside 0.0–1.0 return error string."""
        result = run(read_page_image("basics.pdf", 1, region=[0.0, 0.0, 0.5, 1.5]))
        assert "0.0 and 1.0" in result

    def test_region_validation_ordering(self, indexed_db):
        """x1 >= x2 or y1 >= y2 returns error string."""
        result = run(read_page_image("basics.pdf", 1, region=[0.5, 0.0, 0.3, 1.0]))
        assert "x1 must be < x2" in result


# --- transport selection ---


class TestParseArgs:
    def test_no_args_defaults_to_stdio(self):
        """Existing installs invoke `pdf-search-mcp` with no arguments —
        the default must stay stdio or every current MCP client config
        would break on upgrade."""
        args = _parse_args([])
        assert args.transport == "stdio"

    def test_http_defaults_to_loopback(self):
        """The HTTP transport has no authentication, so the default bind
        must be 127.0.0.1 — exposing it beyond the local machine has to
        be an explicit --host choice."""
        args = _parse_args(["--transport", "http"])
        assert args.host == "127.0.0.1"
        assert args.port == 8000

    def test_http_flags_parsed(self):
        args = _parse_args(
            ["--transport", "http", "--host", "0.0.0.0", "--port", "9000"]
        )
        assert args.transport == "http"
        assert args.host == "0.0.0.0"
        assert args.port == 9000

    def test_unknown_transport_rejected(self):
        """'sse' is a real FastMCP transport but deliberately not exposed
        (deprecated in the MCP spec in favor of streamable HTTP)."""
        with pytest.raises(SystemExit):
            _parse_args(["--transport", "sse"])

    def test_port_out_of_range_rejected(self):
        """argparse type=int accepts 70000; without the explicit range
        check it would only fail later as an OS-level bind error."""
        with pytest.raises(SystemExit):
            _parse_args(["--port", "70000"])
        with pytest.raises(SystemExit):
            _parse_args(["--port", "0"])


class TestMainTransportDispatch:
    def test_default_runs_stdio(self):
        """No arguments must reach mcp.run() without a transport override
        (FastMCP's own default is stdio)."""
        with patch("pdf_search_mcp.mcp_server.mcp") as mock_mcp:
            with patch("sys.argv", ["pdf-search-mcp"]):
                main()
            mock_mcp.run.assert_called_once_with()

    def test_http_sets_bind_address_and_transport(self):
        """--host/--port must land in mcp.settings BEFORE run() — FastMCP
        reads the bind address from settings, not from run() arguments,
        so passing them any other way is silently ignored."""
        with patch("pdf_search_mcp.mcp_server.mcp") as mock_mcp:
            argv = [
                "pdf-search-mcp",
                "--transport", "http",
                "--host", "0.0.0.0",
                "--port", "9000",
            ]
            with patch("sys.argv", argv):
                main()
            assert mock_mcp.settings.host == "0.0.0.0"
            assert mock_mcp.settings.port == 9000
            mock_mcp.run.assert_called_once_with(transport="streamable-http")


# --- stats ---


class TestStats:
    def test_output_contains_fields(self, indexed_db):
        result = run(stats())
        assert "Files:" in result
        assert "Pages:" in result
        assert "DB size:" in result

    def test_no_index_returns_help(self, temp_db):
        """No index should return help message, not raise."""
        result = run(stats())
        assert "No index found" in result
