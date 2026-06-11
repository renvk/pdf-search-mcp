"""Unit tests for the CoreGraphics PDF renderer (macOS only).

Skipped entirely on non-macOS platforms or when PyObjC is missing.
"""

import struct
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS only"
)

try:
    from pdf_search_mcp.render_cg import render_page_coregraphics
except ImportError:
    pytest.skip("pyobjc-framework-Quartz not installed", allow_module_level=True)


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Read (width, height) from a PNG's IHDR chunk (bytes 16-24)."""
    return struct.unpack(">II", data[16:24])


class TestRenderPageCoregraphics:
    """Tests use the make_pdf fixture from conftest (returns a Path)."""

    def test_returns_valid_png(self, make_pdf, tmp_path, png_magic):
        """Render page 1 — output must start with PNG magic bytes."""
        pdf = tmp_path / "cg_test.pdf"
        make_pdf(pdf, "CoreGraphics render test content.")
        data = render_page_coregraphics(str(pdf), 1)
        assert data[:8] == png_magic

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

    def test_zero_dpi_raises(self, make_pdf, tmp_path):
        """dpi=0 used to create a zero-size bitmap context and return
        0 bytes that callers wrote out as a 'successful' PNG."""
        pdf = tmp_path / "cg_zero.pdf"
        make_pdf(pdf, "Zero DPI test.")
        with pytest.raises(ValueError, match="invalid"):
            render_page_coregraphics(str(pdf), 1, dpi=0)

    def test_clip_returns_valid_png(self, make_pdf, tmp_path, png_magic):
        """Clip rect renders only a region — output is valid PNG."""
        pdf = tmp_path / "cg_clip.pdf"
        make_pdf(pdf, "Clip region test content.")
        # Bottom-left quarter in CG coords (bottom-left origin):
        # A4-ish page ≈ 612x792pt, clip = (0, 0, 306, 396)
        data = render_page_coregraphics(
            str(pdf), 1, dpi=150, clip_rect=(0, 0, 306, 396)
        )
        assert data[:8] == png_magic

    def test_clip_produces_smaller_image(self, make_pdf, tmp_path):
        """Clip output should be smaller than full-page at same DPI."""
        pdf = tmp_path / "cg_clip_size.pdf"
        make_pdf(pdf, "Size comparison for clip test.")
        full = render_page_coregraphics(str(pdf), 1, dpi=150)
        clip = render_page_coregraphics(
            str(pdf), 1, dpi=150, clip_rect=(0, 0, 306, 396)
        )
        assert len(clip) < len(full)

    def test_clip_none_renders_full_page(self, make_pdf, tmp_path):
        """clip_rect=None must produce same output as no clip_rect."""
        pdf = tmp_path / "cg_clip_none.pdf"
        make_pdf(pdf, "Clip none test.")
        data_default = render_page_coregraphics(str(pdf), 1, dpi=72)
        data_none = render_page_coregraphics(str(pdf), 1, dpi=72, clip_rect=None)
        assert data_default == data_none


class TestGeometry:
    """Regression tests: the bitmap must be sized from the rotation-applied
    CropBox (PyMuPDF's page.rect), not the MediaBox — otherwise pages with
    CropBox != MediaBox render letterboxed and region crops land on the
    wrong content.
    """

    def test_cropbox_smaller_than_mediabox(self, tmp_path):
        """A 300x400 CropBox inside a 612x792 MediaBox must render at
        CropBox dimensions (previously rendered at full MediaBox size
        with the content letterboxed inside)."""
        import fitz

        pdf = tmp_path / "cg_cropbox.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), "CropBox geometry test.")
        page.set_cropbox(fitz.Rect(0, 0, 300, 400))
        doc.save(str(pdf))
        doc.close()

        data = render_page_coregraphics(str(pdf), 1, dpi=72)
        width, height = _png_dimensions(data)
        assert (width, height) == (300, 400)

    def test_rotated_page_dimensions(self, tmp_path):
        """A /Rotate 90 page must render with swapped dimensions, matching
        fitz page.rect (previously rendered unrotated MediaBox size)."""
        import fitz

        pdf = tmp_path / "cg_rotated.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), "Rotation geometry test.")
        page.set_rotation(90)
        doc.save(str(pdf))
        doc.close()

        data = render_page_coregraphics(str(pdf), 1, dpi=72)
        width, height = _png_dimensions(data)
        assert (width, height) == (792, 612)
