"""Shared fixtures for pdf-search-mcp tests.

Provides isolated temp databases, programmatically-generated sample PDFs,
and a pre-built index for integration tests.
"""

import fitz  # PyMuPDF
import pytest

from pdf_search_mcp.pdf_search import index_pdfs

# First 8 bytes of every valid PNG file — shared by all render tests
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def png_magic():
    """PNG magic bytes as a fixture so test files need no cross-imports."""
    return PNG_MAGIC


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Patch DB_PATH to an isolated temp database for each test."""
    db_path = tmp_path / "test_index.db"
    monkeypatch.setattr("pdf_search_mcp.pdf_search.DB_PATH", db_path)
    return db_path


@pytest.fixture
def sample_pdfs(tmp_path):
    """Create a directory of sample PDFs using PyMuPDF.

    Directory structure:
        pdfs/
            basics.pdf          — 2 pages: English text + German text
            sparse.pdf          — 2 pages: empty page + page with content
            standards/
                EN_13445-3.pdf  — 1 page: pressure equipment text
            _drafts/
                draft.pdf       — 1 page: should be skipped by indexer
    """
    pdfs_dir = tmp_path / "pdfs"
    pdfs_dir.mkdir()

    # basics.pdf — 2 pages: English then German
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This is a test document about pressure vessels and piping.")
    page = doc.new_page()
    page.insert_text((72, 72), "Größe und Außendurchmesser der Schlüsselweite.")
    doc.save(str(pdfs_dir / "basics.pdf"))
    doc.close()

    # sparse.pdf — 2 pages: empty + content
    doc = fitz.open()
    doc.new_page()  # empty page
    page = doc.new_page()
    page.insert_text((72, 72), "Content on page two of sparse document.")
    doc.save(str(pdfs_dir / "sparse.pdf"))
    doc.close()

    # standards/EN_13445-3.pdf — 1 page
    standards_dir = pdfs_dir / "standards"
    standards_dir.mkdir()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "EN 13445-3 Unfired pressure vessels design by analysis.")
    doc.save(str(standards_dir / "EN_13445-3.pdf"))
    doc.close()

    # _drafts/draft.pdf — 1 page (should be skipped)
    drafts_dir = pdfs_dir / "_drafts"
    drafts_dir.mkdir()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Draft content that should not be indexed.")
    doc.save(str(drafts_dir / "draft.pdf"))
    doc.close()

    return pdfs_dir


@pytest.fixture
def indexed_db(temp_db, sample_pdfs):
    """Build an index on sample_pdfs and return (db_path, pdf_dir)."""
    index_pdfs(str(sample_pdfs))
    return temp_db, sample_pdfs


@pytest.fixture
def make_pdf():
    """Factory fixture: create a single-page PDF with the given text.

    Usage: make_pdf(path, "some text content")
    """
    def _make(path, text):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), text)
        doc.save(str(path))
        doc.close()
    return _make
