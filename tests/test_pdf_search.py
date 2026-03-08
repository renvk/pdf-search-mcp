"""Integration tests for pdf_search.py — indexing, search, read, render, stats."""

import sqlite3

import pytest

from pdf_search_mcp.pdf_search import (
    PdfSearchError,
    index_pdfs,
    index_stats,
    read_pdf_page,
    reindex_pdfs,
    render_pdf_page,
    search_pdfs,
)


# --- Indexing ---


class TestIndexPdfs:
    def test_creates_fts5_entries(self, temp_db, sample_pdfs):
        """Verify FTS5 table has rows after indexing."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        conn.close()
        assert count > 0

    def test_skips_drafts_directory(self, temp_db, sample_pdfs):
        """Files under _drafts/ should not be indexed."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        rows = conn.execute("SELECT file FROM pages WHERE file = 'draft.pdf'").fetchall()
        conn.close()
        assert len(rows) == 0

    def test_skips_empty_pages(self, temp_db, sample_pdfs):
        """sparse.pdf page 1 is empty — only page 2 should be indexed."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        rows = conn.execute(
            "SELECT page FROM pages WHERE file = 'sparse.pdf'"
        ).fetchall()
        conn.close()
        pages = [r[0] for r in rows]
        assert 2 in pages
        assert 1 not in pages

    def test_stores_metadata(self, temp_db, sample_pdfs):
        """Index stores pdf_dir, total_files, total_pages, last_indexed in meta."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
        conn.close()
        assert "pdf_dir" in meta
        assert "total_files" in meta
        assert "total_pages" in meta
        assert "last_indexed" in meta

    def test_records_subfolder(self, temp_db, sample_pdfs):
        """EN_13445-3.pdf should have subfolder 'standards'."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        row = conn.execute(
            "SELECT subfolder FROM pages WHERE file = 'EN_13445-3.pdf' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row[0] == "standards"

    def test_nfc_normalizes_filenames(self, temp_db, sample_pdfs):
        """Filenames in DB should be NFC-normalized."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        import unicodedata
        rows = conn.execute("SELECT DISTINCT file FROM pages").fetchall()
        conn.close()
        for (fname,) in rows:
            assert fname == unicodedata.normalize("NFC", fname)

    def test_returns_correct_stats(self, temp_db, sample_pdfs):
        """Return dict with files_indexed and pages_indexed counts."""
        result = index_pdfs(str(sample_pdfs))
        assert result["files_indexed"] == 3  # basics, sparse, EN_13445-3
        assert result["pages_indexed"] == 4  # basics p1+p2, sparse p2, EN_13445-3 p1

    def test_error_no_directory(self, temp_db, monkeypatch):
        """Raise PdfSearchError when no directory specified and env var unset."""
        monkeypatch.delenv("PDF_SEARCH_DIR", raising=False)
        with pytest.raises(PdfSearchError, match="No PDF directory"):
            index_pdfs(None)

    def test_error_nonexistent_directory(self, temp_db):
        """Raise PdfSearchError for a path that doesn't exist."""
        with pytest.raises(PdfSearchError, match="not a directory"):
            index_pdfs("/nonexistent/path")

    def test_error_already_indexed(self, indexed_db):
        """Raise PdfSearchError if index already has data."""
        _, pdf_dir = indexed_db
        with pytest.raises(PdfSearchError, match="already exists"):
            index_pdfs(str(pdf_dir))


# --- Search ---


class TestSearchPdfs:
    def test_finds_content(self, indexed_db):
        """'pressure' appears in basics.pdf and EN_13445-3.pdf."""
        results = search_pdfs("pressure", limit=10)
        files = {r["file"] for r in results}
        assert "basics.pdf" in files or "EN_13445-3.pdf" in files

    def test_snippets_have_markers(self, indexed_db):
        """Snippets should contain >>> and <<< highlight markers."""
        results = search_pdfs("pressure", limit=1)
        assert len(results) >= 1
        assert ">>>" in results[0]["snippet"]
        assert "<<<" in results[0]["snippet"]

    def test_limit_works(self, indexed_db):
        results = search_pdfs("pressure", limit=1)
        assert len(results) <= 1

    def test_no_results(self, indexed_db):
        results = search_pdfs("xyznonexistent", limit=10)
        assert results == []

    def test_no_index_raises(self, temp_db):
        """PdfSearchError when DB doesn't exist."""
        with pytest.raises(PdfSearchError, match="No index found"):
            search_pdfs("test")


# --- Read ---


class TestReadPdfPage:
    def test_returns_expected_text(self, indexed_db):
        """Page 1 of basics.pdf should contain our English test text."""
        text = read_pdf_page("basics.pdf", 1)
        assert "pressure vessels" in text

    def test_page_out_of_range(self, indexed_db):
        with pytest.raises(PdfSearchError, match="out of range"):
            read_pdf_page("basics.pdf", 999)

    def test_page_zero(self, indexed_db):
        with pytest.raises(PdfSearchError, match="out of range"):
            read_pdf_page("basics.pdf", 0)


# --- Render ---


class TestRenderPdfPage:
    def test_returns_existing_png(self, indexed_db):
        path = render_pdf_page("basics.pdf", 1)
        assert path.exists()
        assert str(path).endswith(".png")

    def test_page_out_of_range(self, indexed_db):
        with pytest.raises(PdfSearchError, match="out of range"):
            render_pdf_page("basics.pdf", 999)


# --- Stats ---


class TestIndexStats:
    def test_correct_counts(self, indexed_db):
        info = index_stats()
        assert info["total_files"] == "3"
        assert info["total_pages"] == "4"

    def test_subfolder_breakdown(self, indexed_db):
        info = index_stats()
        assert "standards" in info["subfolders"]
        assert info["subfolders"]["standards"] == 1

    def test_no_index_raises(self, temp_db):
        with pytest.raises(PdfSearchError, match="No index found"):
            index_stats()


# --- Reindex ---


class TestReindexPdfs:
    def test_drops_and_rebuilds(self, indexed_db):
        """Reindex on existing index should succeed."""
        db_path, pdf_dir = indexed_db
        result = reindex_pdfs(str(pdf_dir))
        assert result["files_indexed"] == 3

    def test_works_from_scratch(self, temp_db, sample_pdfs):
        """Reindex when no DB exists should work like a fresh index."""
        result = reindex_pdfs(str(sample_pdfs))
        assert result["files_indexed"] == 3
