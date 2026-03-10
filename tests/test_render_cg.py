"""Unit tests for the CoreGraphics PDF renderer (macOS only).

Skipped entirely on non-macOS platforms or when PyObjC is missing.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS only"
)

try:
    from pdf_search_mcp.render_cg import render_page_coregraphics
except ImportError:
    pytest.skip("pyobjc-framework-Quartz not installed", allow_module_level=True)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestRenderPageCoregraphics:
    """Tests use the make_pdf fixture from conftest (returns a Path)."""

    def test_returns_valid_png(self, make_pdf, tmp_path):
        """Render page 1 — output must start with PNG magic bytes."""
        pdf = tmp_path / "cg_test.pdf"
        make_pdf(pdf, "CoreGraphics render test content.")
        data = render_page_coregraphics(str(pdf), 1)
        assert data[:8] == _PNG_MAGIC

    def test_higher_dpi_produces_larger_image(self, make_pdf, tmp_path):
        """300 DPI output should be larger than 72 DPI for the same page."""
        pdf = tmp_path / "cg_dpi.pdf"
        make_pdf(pdf, "DPI comparison test.")
        lo = render_page_coregraphics(str(pdf), 1, dpi=72)
        hi = render_page_coregraphics(str(pdf), 1, dpi=300)
        assert len(hi) > len(lo)

    def test_invalid_path_raises(self):
        """Nonexistent PDF path raises ValueError."""
        with pytest.raises(ValueError, match="Cannot open PDF"):
            render_page_coregraphics("/nonexistent/file.pdf", 1)

    def test_invalid_page_raises(self, make_pdf, tmp_path):
        """Page number beyond document length raises ValueError."""
        pdf = tmp_path / "cg_page.pdf"
        make_pdf(pdf, "Single page.")
        with pytest.raises(ValueError, match="out of range"):
            render_page_coregraphics(str(pdf), 999)
