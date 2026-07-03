"""Integration tests for pdf_search.py — indexing, search, read, render, stats."""

import os
import sqlite3

import pytest

from pdf_search_mcp.pdf_search import (
    PdfSearchError,
    _HL_CLOSE,
    _HL_OPEN,
    _MAX_RENDER_EDGE_PX,
    _compute_region_dpi,
    _density_components,
    _normalize_text,
    index_pdfs,
    index_stats,
    read_pdf_page,
    reindex_pdfs,
    render_pdf_page,
    search_pdfs,
    search_with_relaxation,
)


# --- Text normalization ---


class TestNormalizeText:
    def test_decomposes_ligatures(self):
        """FTS5's unicode61 tokenizer does not fold ligature codepoints —
        without decomposition, 'eﬃciency' never matches the query
        'efficiency'. All seven Alphabetic Presentation Forms ligatures
        must decompose."""
        assert (
            _normalize_text("high eﬃciency ﬂow deﬁne oﬀset waﬄe ﬅill ﬆop")
            == "high efficiency flow define offset waffle ftill stop"
        )

    def test_joins_hyphenated_line_break(self):
        """A word split by line-break hyphenation is indexed as two junk
        tokens ('Druckbehäl' + 'ter') — the query 'Druckbehälter' misses
        it. Lowercase continuation marks a split word, so the fragments
        are joined and the consumed newline collapses the two lines."""
        assert (
            _normalize_text("Der Druckbehäl-\nter ist geprüft.")
            == "Der Druckbehälter ist geprüft."
        )

    def test_joins_consecutive_hyphen_breaks(self):
        """A long compound can wrap twice; sequential re.sub scanning must
        join both breaks, not just the first."""
        assert (
            _normalize_text("Wärmeübertra-\ngungsflä-\nche")
            == "Wärmeübertragungsfläche"
        )

    def test_keeps_hyphen_before_uppercase(self):
        """'AD 2000-\\nMerkblatt' is a genuine hyphenated compound wrapped
        at its hyphen — joining would create the junk token
        '2000Merkblatt'. Uppercase continuation must keep the hyphen
        (and the line break) intact."""
        text = "Das AD 2000-\nMerkblatt B1 gilt."
        assert _normalize_text(text) == text

    def test_keeps_hyphen_before_digit(self):
        """Standard designations wrap at their hyphen with a digit
        continuation ('EN 13445-\\n3') — digits are not lowercase, so the
        hyphen must survive."""
        text = "Nach EN 13445-\n3 berechnet."
        assert _normalize_text(text) == text

    def test_keeps_hyphen_after_digit(self):
        """A digit before the hyphen ('2000-\\nter') cannot be a split
        word — group 1 requires a letter, so nothing is joined even
        though the continuation is lowercase."""
        text = "Ausgabe 2000-\nter Teil."
        assert _normalize_text(text) == text

    def test_strips_soft_hyphens(self):
        """Soft hyphens (U+00AD) are invisible in print but split FTS5
        tokens — 'Druck\\u00adbehälter' never matches 'Druckbehälter'."""
        assert _normalize_text("Druck­behälter") == "Druckbehälter"

    def test_soft_hyphen_adjacent_to_hyphen_break(self):
        """Publisher PDFs emit a soft hyphen next to the printed hyphen at
        a line break ('ex', U+00AD, '-', newline — observed byte-for-byte
        in Cengel-Cimbala). Stripping soft hyphens after the join regex
        leaves the fresh '-\n' unjoined, so the strip must run first.
        Both orderings around the printed hyphen must join."""
        assert _normalize_text("is ex\u00ad-\npressed as") == "is expressed as"
        assert _normalize_text("is ex-\u00ad\npressed as") == "is expressed as"

    def test_plain_text_unchanged(self):
        """Text with none of the three defects must pass through
        byte-identical — normalization must not touch layout whitespace."""
        text = "This is a test document.\nSecond line with Größe.\n"
        assert _normalize_text(text) == text


class TestNormalizationEndToEnd:
    @pytest.fixture
    def hyphenated_pdf_dir(self, tmp_path):
        """One PDF whose page text wraps 'Druckbehälter' across two lines
        with a hyphen — two insert_text calls 14pt apart extract as
        'Druckbehäl-\\nter' (verified against PyMuPDF), reproducing real
        line-break hyphenation."""
        import fitz

        pdfs_dir = tmp_path / "hyphen_pdfs"
        pdfs_dir.mkdir()
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Der Druckbehäl-")
        page.insert_text((72, 86), "ter ist geprüft.")
        doc.save(str(pdfs_dir / "hyphen.pdf"))
        doc.close()
        return pdfs_dir

    def test_search_finds_dehyphenated_compound(self, temp_db, hyphenated_pdf_dir):
        """The whole point of dehyphenation: the joined compound must be
        findable by full-text search."""
        index_pdfs(str(hyphenated_pdf_dir))
        results = search_pdfs("Druckbehälter")
        assert len(results) == 1
        assert results[0]["file"] == "hyphen.pdf"

    def test_read_page_matches_indexed_text(self, temp_db, hyphenated_pdf_dir):
        """Module invariant: read_pdf_page must return exactly the text
        that was indexed, so a search hit is always visible in the page
        the agent then reads."""
        index_pdfs(str(hyphenated_pdf_dir))
        page_text = read_pdf_page("hyphen.pdf", 1)
        assert "Druckbehälter" in page_text
        conn = sqlite3.connect(str(temp_db))
        indexed = conn.execute(
            "SELECT content FROM pages WHERE file = 'hyphen.pdf'"
        ).fetchone()[0]
        conn.close()
        assert page_text == indexed


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
        """Index stores pdf_dir and last_indexed in meta. Totals are NOT
        cached — they are derived from the tables at read time so they
        cannot go stale after an interrupted run."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
        conn.close()
        assert "pdf_dir" in meta
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
        """Fresh index: all files reported as added, totals match."""
        result = index_pdfs(str(sample_pdfs))
        assert result["files_added"] == 3  # basics, sparse, EN_13445-3
        assert result["total_files"] == 3
        assert result["total_pages"] == 4  # basics p1+p2, sparse p2, EN_13445-3 p1
        assert result["files_updated"] == 0
        assert result["files_deleted"] == 0

    def test_recovers_pdf_dir_from_meta(self, indexed_db, monkeypatch):
        """index_pdfs() with no arg and no env var recovers path from meta table."""
        monkeypatch.delenv("PDF_SEARCH_DIR", raising=False)
        result = index_pdfs()
        assert result["total_files"] == 3
        assert result["files_unchanged"] == 3

    def test_error_no_directory(self, temp_db, monkeypatch):
        """Raise PdfSearchError when no directory specified, no env var, no DB."""
        monkeypatch.delenv("PDF_SEARCH_DIR", raising=False)
        with pytest.raises(PdfSearchError, match="No PDF directory"):
            index_pdfs(None)

    def test_error_empty_env_var(self, temp_db, monkeypatch):
        """An empty PDF_SEARCH_DIR must error, not index the CWD —
        Path('').resolve() is the current working directory."""
        monkeypatch.setenv("PDF_SEARCH_DIR", "")
        with pytest.raises(PdfSearchError, match="No PDF directory"):
            index_pdfs(None)

    def test_error_whitespace_pdf_dir_argument(self, temp_db, monkeypatch):
        """A whitespace-only argument is treated as unset, same as the env var."""
        monkeypatch.delenv("PDF_SEARCH_DIR", raising=False)
        with pytest.raises(PdfSearchError, match="No PDF directory"):
            index_pdfs("   ")

    def test_error_nonexistent_directory(self, temp_db):
        """Raise PdfSearchError for a path that doesn't exist."""
        with pytest.raises(PdfSearchError, match="not a directory"):
            index_pdfs("/nonexistent/path")

    def test_noop_when_unchanged(self, indexed_db):
        """Calling index_pdfs() again with no changes is a no-op."""
        _, pdf_dir = indexed_db
        result = index_pdfs(str(pdf_dir))
        assert result["files_added"] == 0
        assert result["files_updated"] == 0
        assert result["files_deleted"] == 0
        assert result["files_unchanged"] == 3
        assert result["total_files"] == 3
        assert result["total_pages"] == 4

    def test_dangling_symlink_does_not_abort_sync(self, temp_db, sample_pdfs):
        """A broken symlink named *.pdf is recorded as a per-file error;
        the remaining PDFs must still be indexed (previously the unguarded
        stat() aborted the whole run with FileNotFoundError)."""
        (sample_pdfs / "dangling.pdf").symlink_to(sample_pdfs / "no-such-target.pdf")
        result = index_pdfs(str(sample_pdfs))
        assert result["files_added"] == 3
        assert any("dangling.pdf" in fname for fname, _ in result["errors"])

    def test_partial_extraction_failure_leaves_no_orphan_pages(
        self, temp_db, sample_pdfs, monkeypatch
    ):
        """A PDF failing mid-extraction must roll back its partial page
        inserts. Previously the pages stayed without a files row, so every
        rerun re-inserted them — duplicate rows accumulated."""
        def failing_index(conn, filepath, fname_nfc, subfolder):
            conn.execute(
                "INSERT INTO pages (file, subfolder, page, content) VALUES (?, ?, ?, ?)",
                (fname_nfc, subfolder, 1, "partial content"),
            )
            raise RuntimeError("extraction failed mid-file")

        monkeypatch.setattr(
            "pdf_search_mcp.pdf_search._index_single_pdf", failing_index
        )
        for run in (1, 2):
            result = index_pdfs(str(sample_pdfs))
            assert len(result["errors"]) == 3
            conn = sqlite3.connect(str(temp_db))
            orphans = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            conn.close()
            assert orphans == 0, f"run {run} left orphan page rows"

    def test_outdated_schema_rejected(self, temp_db, sample_pdfs):
        """Incremental writes into a v1 table (searchable file/page columns)
        would mix schemas — index must demand a reindex instead."""
        conn = sqlite3.connect(str(temp_db))
        conn.execute(
            "CREATE VIRTUAL TABLE pages USING fts5(file, subfolder, page, content)"
        )
        conn.commit()
        conn.close()
        with pytest.raises(PdfSearchError, match="outdated schema"):
            index_pdfs(str(sample_pdfs))


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

    def test_results_have_only_public_keys(self, indexed_db):
        """Internal ranking fields must not leak into tool output."""
        results = search_pdfs("pressure", limit=1)
        assert set(results[0]) == {"file", "subfolder", "page", "snippet"}

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

    def test_negative_limit_raises(self, indexed_db):
        """limit=-1 would become SQL 'LIMIT -3' (unlimited fetch) and the
        final slice would silently drop the last result."""
        with pytest.raises(PdfSearchError, match="positive integer"):
            search_pdfs("pressure", limit=-1)

    def test_zero_limit_raises(self, indexed_db):
        with pytest.raises(PdfSearchError, match="positive integer"):
            search_pdfs("pressure", limit=0)

    def test_filename_tokens_do_not_match(self, indexed_db):
        """The file column is UNINDEXED: searching 'basics' (a filename,
        not page content) must return nothing. Previously every query term
        could match filenames and page numbers, polluting results."""
        assert search_pdfs("basics", limit=10) == []

    def test_invalid_db_file_raises_clear_error(self, temp_db, monkeypatch):
        """PDF_SEARCH_DB pointing at a non-database file must produce a
        PdfSearchError, not a raw sqlite3.DatabaseError."""
        temp_db.write_text("this is not a sqlite database")
        with pytest.raises(PdfSearchError, match="not a valid search index"):
            search_pdfs("test")

    def test_outdated_schema_raises(self, temp_db):
        """Searching a v1 index must demand a reindex."""
        conn = sqlite3.connect(str(temp_db))
        conn.execute(
            "CREATE VIRTUAL TABLE pages USING fts5(file, subfolder, page, content)"
        )
        conn.commit()
        conn.close()
        with pytest.raises(PdfSearchError, match="outdated schema"):
            search_pdfs("test")

    def test_db_path_with_hash_character(self, tmp_path, monkeypatch, sample_pdfs):
        """A '#' in the DB path must not be parsed as a URI fragment —
        previously every read-only connection opened (and created) a
        truncated path instead of the real database."""
        db_dir = tmp_path / "c#dir"
        db_dir.mkdir()
        db_path = db_dir / "pdf_index.db"
        monkeypatch.setattr("pdf_search_mcp.pdf_search.DB_PATH", db_path)
        index_pdfs(str(sample_pdfs))
        results = search_pdfs("pressure", limit=5)
        assert results
        assert not (tmp_path / "c").exists()  # no stray truncated file


# --- Search with relaxation (prepared user queries) ---


class TestSearchWithRelaxation:
    def test_direct_match_no_note(self, indexed_db):
        results, note = search_with_relaxation("pressure vessels", 10)
        assert results
        assert note == ""

    def test_german_expansion_applies(self, indexed_db):
        """'Aussendurchmesser' must match the page containing
        'Außendurchmesser' via reverse digraph expansion (one position
        at a time, so a single-conversion word is used)."""
        results, note = search_with_relaxation("Aussendurchmesser", 10)
        assert any(r["file"] == "basics.pdf" for r in results)

    def test_drops_least_represented_term(self, indexed_db):
        """3+ terms with one nonexistent: phase 1 drops the term whose
        removal yields the most matches corpus-wide."""
        results, note = search_with_relaxation("pressure vessels xyznonexistent", 10)
        assert results
        assert "Relaxed to" in note
        assert "xyznonexistent" not in note  # dropped term excluded from note

    def test_two_terms_or_fallback(self, indexed_db):
        """With 2 terms, phase 1 is skipped and the OR fallback runs."""
        results, note = search_with_relaxation("pressure xyznonexistent", 10)
        assert results
        assert "any term" in note

    def test_all_terms_missing(self, indexed_db):
        results, note = search_with_relaxation("qqq zzz xxx", 10)
        assert results == []
        assert note == ""

    def test_structured_query_not_relaxed(self, indexed_db):
        """Explicit operators bypass relaxation entirely."""
        results, note = search_with_relaxation("xyznonexistent AND pressure", 10)
        assert results == []
        assert note == ""

    def test_colon_term_searches_cleanly(self, indexed_db):
        """Bug #Q1 end-to-end: '1:100' used to raise 'no such column: 1'
        straight through the tool layer."""
        results, note = search_with_relaxation("Maßstab 1:100", 10)
        assert isinstance(results, list)  # no exception is the assertion

    def test_no_searchable_terms_raises(self, indexed_db):
        """Pure punctuation prepares to '' — a clear error, not silence."""
        with pytest.raises(PdfSearchError, match="no searchable terms"):
            search_with_relaxation("()", 10)

    def test_unsearchable_term_does_not_break_relaxation(self, indexed_db):
        """PR-review issue 1: 'xyznonexistent *' built the OR fallback as
        'xyznonexistent OR' (the lone '*' prepares to nothing) and raised
        a syntax error quoting a query the user never typed. It must
        degrade to plain no-results."""
        results, note = search_with_relaxation("xyznonexistent *", 10)
        assert results == []
        assert note == ""

    def test_invalid_fts5_surfaces_as_pdf_search_error(self, indexed_db):
        """'NOT term' is invalid FTS5 (NOT is binary). The error must
        surface as PdfSearchError, never as silent zero results — masking
        syntax errors as 'no matches' misleads the caller about the corpus."""
        with pytest.raises(PdfSearchError, match="Search failed"):
            search_with_relaxation("NOT pressure", 10)


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

    def test_corrupted_file_raises_pdf_search_error(self, indexed_db):
        """A file corrupted after indexing must raise PdfSearchError, not
        leak fitz.FileDataError through the tool layer."""
        _, pdf_dir = indexed_db
        (pdf_dir / "basics.pdf").write_bytes(b"no longer a pdf")
        with pytest.raises(PdfSearchError, match="Cannot open"):
            read_pdf_page("basics.pdf", 1)


# --- Duplicate filename resolution ---


class TestDuplicateResolution:
    @pytest.fixture
    def dup_index(self, temp_db, sample_pdfs, make_pdf):
        """Same filename in root and in sub/ with distinct content."""
        make_pdf(sample_pdfs / "dup.pdf", "ROOT copy content marker")
        sub = sample_pdfs / "sub"
        sub.mkdir()
        make_pdf(sub / "dup.pdf", "SUB copy content marker")
        index_pdfs(str(sample_pdfs))
        return sample_pdfs

    def test_unspecified_subfolder_with_duplicates_raises(self, dup_index):
        """An arbitrary pick would silently return the wrong document —
        the previous SELECT ... LIMIT 1 had no ORDER BY."""
        with pytest.raises(PdfSearchError, match="Multiple files named"):
            read_pdf_page("dup.pdf", 1)

    def test_empty_string_selects_root_copy(self, dup_index):
        """'' is the stored subfolder value for root files. Previously the
        wrapper coerced '' to None, making the root copy unselectable."""
        text = read_pdf_page("dup.pdf", 1, subfolder="")
        assert "ROOT copy" in text

    def test_subfolder_selects_sub_copy(self, dup_index):
        text = read_pdf_page("dup.pdf", 1, subfolder="sub")
        assert "SUB copy" in text

    def test_unique_file_needs_no_subfolder(self, dup_index):
        """Non-duplicated files keep resolving without a subfolder."""
        text = read_pdf_page("basics.pdf", 1)
        assert "pressure vessels" in text


# --- Image-only PDFs ---


class TestImageOnlyPdf:
    def test_resolvable_despite_zero_text_pages(self, temp_db, sample_pdfs):
        """A scanned (image-only) PDF has a files row but no pages rows.
        Resolution now uses the files table, so read/render must work —
        read_page_image is exactly the tool needed for scanned PDFs."""
        doc_path = sample_pdfs / "imageonly.pdf"
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(50, 50, 200, 200), color=(0, 0, 0), fill=(1, 0, 0))
        doc.save(str(doc_path))
        doc.close()

        index_pdfs(str(sample_pdfs))
        assert read_pdf_page("imageonly.pdf", 1) == ""
        path = render_pdf_page("imageonly.pdf", 1)
        assert path.exists()


# --- Render ---


class TestRenderPdfPage:
    def test_returns_existing_png(self, indexed_db):
        path = render_pdf_page("basics.pdf", 1)
        assert path.exists()
        assert str(path).endswith(".png")

    def test_render_output_is_valid_png(self, indexed_db, png_magic):
        """Rendered file starts with PNG magic bytes regardless of renderer."""
        path = render_pdf_page("basics.pdf", 1)
        with open(path, "rb") as f:
            magic = f.read(8)
        assert magic == png_magic

    def test_pymupdf_fallback(self, indexed_db, monkeypatch, png_magic):
        """With CG disabled, PyMuPDF fallback still produces a valid PNG."""
        import pdf_search_mcp.pdf_search as mod
        monkeypatch.setattr(mod, "_USE_COREGRAPHICS", False)
        path = render_pdf_page("basics.pdf", 1)
        with open(path, "rb") as f:
            magic = f.read(8)
        assert magic == png_magic

    def test_page_out_of_range(self, indexed_db):
        with pytest.raises(PdfSearchError, match="out of range"):
            render_pdf_page("basics.pdf", 999)

    def test_zero_dpi_raises(self, indexed_db):
        """dpi=0 produced a zero-size CG bitmap and a 0-byte PNG that was
        reported as a successful render."""
        with pytest.raises(PdfSearchError, match="dpi must be"):
            render_pdf_page("basics.pdf", 1, dpi=0)

    def test_zero_area_region_raises(self, indexed_db):
        """A zero-area region divided by zero in auto-DPI when called via
        the public API (validation only existed in the MCP tool layer)."""
        with pytest.raises(PdfSearchError, match="x1 must be < x2"):
            render_pdf_page("basics.pdf", 1, region=[0.5, 0.5, 0.5, 0.5])

    def test_non_sequence_region_raises_pdf_search_error(self, indexed_db):
        """Review minor: a non-sequence region raised TypeError from len()
        via the Python API; the docstring promises PdfSearchError."""
        with pytest.raises(PdfSearchError, match="4 floats"):
            render_pdf_page("basics.pdf", 1, region=0.5)

    def test_non_numeric_region_raises_pdf_search_error(self, indexed_db):
        with pytest.raises(PdfSearchError, match="between 0.0 and 1.0"):
            render_pdf_page("basics.pdf", 1, region=[0.0, "a", 0.5, 0.5])

    def test_zero_dpi_ignored_when_region_set(self, indexed_db):
        """dpi is documented as ignored for region renders — a bad dpi
        value must not reject a call that never uses it."""
        path = render_pdf_page("basics.pdf", 1, dpi=0, region=[0.0, 0.0, 0.5, 0.5])
        assert path.exists()

    def test_region_out_of_bounds_raises(self, indexed_db):
        with pytest.raises(PdfSearchError, match="between 0.0 and 1.0"):
            render_pdf_page("basics.pdf", 1, region=[0.0, 0.0, 0.5, 1.5])

    def test_region_wrong_length_raises(self, indexed_db):
        with pytest.raises(PdfSearchError, match="4 floats"):
            render_pdf_page("basics.pdf", 1, region=[0.0, 0.0, 0.5])

    def test_region_returns_valid_png(self, indexed_db, png_magic):
        """Region crop produces a valid PNG file."""
        path = render_pdf_page("basics.pdf", 1, region=[0.0, 0.0, 0.5, 0.5])
        assert path.exists()
        with open(path, "rb") as f:
            assert f.read(8) == png_magic

    def test_region_filename_includes_coords(self, indexed_db):
        """Output filename includes region tag to avoid cache collisions."""
        path = render_pdf_page("basics.pdf", 1, region=[0.1, 0.2, 0.8, 0.9])
        assert "_r0.10_0.20_0.80_0.90" in str(path)

    def test_region_with_pymupdf_fallback(self, indexed_db, monkeypatch, png_magic):
        """Region rendering works via PyMuPDF when CG is disabled."""
        import pdf_search_mcp.pdf_search as mod
        monkeypatch.setattr(mod, "_USE_COREGRAPHICS", False)
        path = render_pdf_page("basics.pdf", 1, region=[0.0, 0.0, 1.0, 0.5])
        with open(path, "rb") as f:
            assert f.read(8) == png_magic

    def test_region_none_renders_full_page(self, indexed_db):
        """Explicit region=None behaves like no region (full page)."""
        path = render_pdf_page("basics.pdf", 1, region=None)
        assert path.exists()
        assert "_r0" not in path.name

    def test_duplicate_filenames_render_to_distinct_paths(
        self, temp_db, sample_pdfs, make_pdf
    ):
        """Renders of same-named files in different subfolders must not
        overwrite each other — the output name now hashes the full source
        path. Previously the second render clobbered the first."""
        make_pdf(sample_pdfs / "dup.pdf", "ROOT copy")
        sub = sample_pdfs / "sub"
        sub.mkdir()
        make_pdf(sub / "dup.pdf", "SUB copy")
        index_pdfs(str(sample_pdfs))

        path_root = render_pdf_page("dup.pdf", 1, subfolder="")
        path_sub = render_pdf_page("dup.pdf", 1, subfolder="sub")
        assert path_root != path_sub

    def test_repeated_render_reuses_cached_file(self, indexed_db):
        """Same page/DPI/region returns the cached PNG without re-rendering
        (observable via unchanged mtime)."""
        path1 = render_pdf_page("basics.pdf", 1)
        mtime1 = path1.stat().st_mtime_ns
        path2 = render_pdf_page("basics.pdf", 1)
        assert path1 == path2
        assert path2.stat().st_mtime_ns == mtime1

    def test_nearby_regions_get_distinct_cache_paths(self, indexed_db):
        """PR-review issue 2: regions differing only past the 2nd decimal
        (0.100 vs 0.104) rounded to the same filename, and the cache-hit
        early return then served the first crop's pixels for the second
        region. The path now hashes full-precision coordinates."""
        path_a = render_pdf_page("basics.pdf", 1, region=[0.100, 0.1, 0.5, 0.5])
        path_b = render_pdf_page("basics.pdf", 1, region=[0.104, 0.1, 0.5, 0.5])
        assert path_a != path_b


# --- Compute Region DPI ---


class TestComputeRegionDpi:
    """Unit tests for _compute_region_dpi auto-scaling logic."""

    def test_full_page_a4(self):
        """Full-page region on A4 (595x842pt) → ~134 DPI."""
        dpi = _compute_region_dpi(595, 842, [0, 0, 1, 1], 600)
        # 1568 * 72 / 842 ≈ 134
        assert dpi == 134

    def test_half_page_height(self):
        """Top half of A4 → long edge is width (595pt), ~189 DPI."""
        dpi = _compute_region_dpi(595, 842, [0, 0, 1.0, 0.5], 600)
        # crop is 595 x 421 pt, long edge 595, dpi = 1568*72/595 ≈ 189
        assert dpi == 189

    def test_quarter_page(self):
        """Top-left quarter → long edge 421pt, ~267 DPI."""
        dpi = _compute_region_dpi(595, 842, [0, 0, 0.5, 0.5], 600)
        # crop is 297.5 x 421 pt, long edge 421, dpi = 1568*72/421 ≈ 268
        assert dpi == 268

    def test_capped_at_dpi_param(self):
        """When computed DPI exceeds cap, use the cap."""
        # Tiny region: 10% x 10% of A4 → long edge 84.2pt → ~1340 DPI
        dpi = _compute_region_dpi(595, 842, [0, 0, 0.1, 0.1], 600)
        assert dpi == 600

    def test_capped_at_lower_custom_dpi(self):
        """Caller-specified cap below computed DPI takes precedence."""
        dpi = _compute_region_dpi(595, 842, [0, 0, 0.5, 0.5], 200)
        assert dpi == 200

    def test_output_below_target(self):
        """Rendered long edge must not exceed _MAX_RENDER_EDGE_PX.

        int() truncation ensures we stay at or below the threshold.
        """
        dpi = _compute_region_dpi(595, 842, [0, 0, 1, 1], 600)
        long_edge_pt = 842
        pixels = long_edge_pt * dpi / 72
        assert pixels <= _MAX_RENDER_EDGE_PX


# --- Stats ---


class TestIndexStats:
    def test_correct_counts(self, indexed_db):
        """Counts come from the tables directly, as integers."""
        info = index_stats()
        assert info["total_files"] == 3
        assert info["total_pages"] == 4

    def test_renderer_field(self, indexed_db):
        """Stats include active renderer name (CoreGraphics or PyMuPDF)."""
        info = index_stats()
        assert info["renderer"] in ("CoreGraphics", "PyMuPDF")

    def test_subfolder_breakdown(self, indexed_db):
        info = index_stats()
        assert "standards" in info["subfolders"]
        assert info["subfolders"]["standards"] == 1

    def test_no_index_raises(self, temp_db):
        with pytest.raises(PdfSearchError, match="No index found"):
            index_stats()

    def test_invalid_db_file_raises_clear_error(self, temp_db):
        """A non-database file must raise PdfSearchError, not DatabaseError."""
        temp_db.write_text("not a database")
        with pytest.raises(PdfSearchError, match="not a valid search index"):
            index_stats()


# --- Reindex ---


class TestReindexPdfs:
    def test_drops_and_rebuilds(self, indexed_db):
        """Reindex on existing index should succeed."""
        db_path, pdf_dir = indexed_db
        result = reindex_pdfs(str(pdf_dir))
        assert result["total_files"] == 3
        assert result["files_added"] == 3

    def test_works_from_scratch(self, temp_db, sample_pdfs):
        """Reindex when no DB exists should work like a fresh index."""
        result = reindex_pdfs(str(sample_pdfs))
        assert result["total_files"] == 3
        assert result["files_added"] == 3

    def test_recovers_pdf_dir_from_meta(self, indexed_db, monkeypatch):
        """Reindex with no pdf_dir arg and no env var recovers path from meta table.

        Regression: previously reindex_pdfs deleted the DB first, losing the
        stored pdf_dir and raising PdfSearchError.
        """
        monkeypatch.delenv("PDF_SEARCH_DIR", raising=False)
        result = reindex_pdfs()
        assert result["total_files"] == 3
        assert result["files_added"] == 3

    def test_db_deleted_on_reindex_failure(self, indexed_db, monkeypatch):
        """If the stored pdf_dir points to a missing directory, the DB is still
        deleted (reindex is destructive) and a clear error is raised.

        Verifies the DB does not survive a failed reindex — no stale index.
        """
        db_path, _ = indexed_db
        # Point stored pdf_dir at a nonexistent directory
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE meta SET value = '/nonexistent/path' WHERE key = 'pdf_dir'"
        )
        conn.commit()
        conn.close()

        monkeypatch.delenv("PDF_SEARCH_DIR", raising=False)
        with pytest.raises(PdfSearchError):
            reindex_pdfs()
        assert not db_path.exists()

    def test_rebuilds_outdated_schema(self, temp_db, sample_pdfs):
        """reindex is the documented migration path from the v1 schema."""
        conn = sqlite3.connect(str(temp_db))
        conn.execute(
            "CREATE VIRTUAL TABLE pages USING fts5(file, subfolder, page, content)"
        )
        conn.commit()
        conn.close()
        result = reindex_pdfs(str(sample_pdfs))
        assert result["total_files"] == 3


# --- Incremental Indexing ---


class TestIncrementalIndex:
    """Tests for incremental sync: add, delete, change, and mixed scenarios."""

    def test_new_file_added(self, indexed_db, make_pdf):
        """A new PDF on disk is detected and indexed."""
        _, pdf_dir = indexed_db
        make_pdf(pdf_dir / "extras.pdf", "Extra content about flanges.")

        result = index_pdfs(str(pdf_dir))
        assert result["files_added"] == 1
        assert result["files_deleted"] == 0
        assert result["files_updated"] == 0
        assert result["total_files"] == 4
        # new content is searchable
        hits = search_pdfs("flanges")
        assert any(r["file"] == "extras.pdf" for r in hits)

    def test_file_deleted(self, indexed_db):
        """A PDF removed from disk is removed from the index."""
        _, pdf_dir = indexed_db
        (pdf_dir / "basics.pdf").unlink()

        result = index_pdfs(str(pdf_dir))
        assert result["files_deleted"] == 1
        assert result["files_added"] == 0
        assert result["total_files"] == 2
        # deleted file no longer searchable
        hits = search_pdfs("pressure vessels")
        assert not any(r["file"] == "basics.pdf" for r in hits)

    def test_file_changed(self, indexed_db, make_pdf):
        """A modified PDF (different size) is re-extracted."""
        _, pdf_dir = indexed_db
        # overwrite basics.pdf with different content
        make_pdf(pdf_dir / "basics.pdf", "Completely new content about turbines.")

        result = index_pdfs(str(pdf_dir))
        assert result["files_updated"] == 1
        assert result["files_added"] == 0
        assert result["files_deleted"] == 0
        # old content gone, new content searchable
        hits = search_pdfs("turbines")
        assert any(r["file"] == "basics.pdf" for r in hits)
        hits = search_pdfs("pressure vessels")
        assert not any(r["file"] == "basics.pdf" for r in hits)

    def test_mixed_changes(self, indexed_db, make_pdf):
        """Simultaneous add, delete, and update in one sync."""
        _, pdf_dir = indexed_db
        # delete sparse.pdf
        (pdf_dir / "sparse.pdf").unlink()
        # modify basics.pdf
        make_pdf(pdf_dir / "basics.pdf", "Updated basics about nozzles.")
        # add new.pdf
        make_pdf(pdf_dir / "new.pdf", "Brand new document about gaskets.")

        result = index_pdfs(str(pdf_dir))
        assert result["files_added"] == 1
        assert result["files_deleted"] == 1
        assert result["files_updated"] == 1
        assert result["files_unchanged"] == 1  # EN_13445-3.pdf
        assert result["total_files"] == 3  # was 3, -1 +1 = 3

    def test_file_moved_to_drafts(self, indexed_db):
        """Moving a PDF into _drafts/ is treated as a deletion."""
        _, pdf_dir = indexed_db
        drafts = pdf_dir / "_drafts"
        (pdf_dir / "basics.pdf").rename(drafts / "basics.pdf")

        result = index_pdfs(str(pdf_dir))
        assert result["files_deleted"] == 1
        assert result["total_files"] == 2

    def test_files_table_populated_on_fresh_index(self, temp_db, sample_pdfs):
        """Fresh index populates the files table with mtime and size."""
        index_pdfs(str(sample_pdfs))
        conn = sqlite3.connect(str(temp_db))
        rows = conn.execute("SELECT file, subfolder, mtime, size FROM files").fetchall()
        conn.close()
        assert len(rows) == 3
        # all rows have positive mtime and size
        for _, _, mtime, size in rows:
            assert mtime > 0
            assert size > 0

    def test_corrupted_update_preserves_old_pages(self, indexed_db):
        """If re-indexing a changed file fails, old pages stay in the index."""
        _, pdf_dir = indexed_db
        # Replace basics.pdf with a corrupted (non-PDF) file to trigger an error
        corrupted = pdf_dir / "basics.pdf"
        original_size = corrupted.stat().st_size
        corrupted.write_bytes(b"not a valid pdf file content here")
        # Ensure size differs so change is detected
        assert corrupted.stat().st_size != original_size

        result = index_pdfs(str(pdf_dir))
        assert len(result["errors"]) == 1
        assert result["files_updated"] == 1  # attempted update
        # Old content should still be searchable because rollback restored it
        hits = search_pdfs("pressure vessels")
        assert any(r["file"] == "basics.pdf" for r in hits)


# --- Density Ranking ---


class TestDensityRanking:
    """Tests for _density_components() and density-aware re-ranking."""

    def test_density_components_multiple_close(self):
        """Markers clustered together yield high clustering."""
        text = (
            f"{_HL_OPEN}a{_HL_CLOSE} {_HL_OPEN}b{_HL_CLOSE} {_HL_OPEN}c{_HL_CLOSE}"
            " some filler text that goes on for a while"
        )
        cd, cl = _density_components(text)
        assert cd > 0
        # all markers in the first ~30 chars of a ~65-char string → high clustering
        assert cl > 0.6

    def test_density_components_multiple_spread(self):
        """Markers spread across the text yield low clustering."""
        text = (
            f"{_HL_OPEN}a{_HL_CLOSE}" + " " * 200
            + f"{_HL_OPEN}b{_HL_CLOSE}" + " " * 200
            + f"{_HL_OPEN}c{_HL_CLOSE}"
        )
        cd, cl = _density_components(text)
        # span covers nearly the entire string → low clustering
        assert cl < 0.1

    def test_density_components_single_match(self):
        """One marker returns neutral clustering (0.5)."""
        text = f"some text with {_HL_OPEN}one{_HL_CLOSE} match"
        cd, cl = _density_components(text)
        assert cd > 0
        assert cl == 0.5

    def test_density_components_no_markers(self):
        """No markers returns zero density, neutral clustering."""
        text = "plain text with no matches"
        cd, cl = _density_components(text)
        assert cd == 0.0
        assert cl == 0.5

    def test_literal_angle_markers_do_not_count(self):
        """Pages whose TEXT contains literal '>>>' (shell transcripts,
        quoted email) must not inflate the density score — counting now
        uses control-character sentinels that never occur in page text."""
        text = ">>> >>> >>> literal shell prompt lines, zero real matches"
        cd, cl = _density_components(text)
        assert cd == 0.0

    def test_dense_page_ranks_higher(self, temp_db, make_pdf, tmp_path):
        """A page with dense term occurrences outranks a page with one mention.

        Creates two PDFs: one with 'turbine' repeated densely in a short
        paragraph, one with a single 'turbine' mention in longer text.
        The dense page should rank first after density re-ranking.
        """
        pdf_dir = tmp_path / "density_pdfs"
        pdf_dir.mkdir()

        # Dense page: 'turbine' repeated many times in a short block
        make_pdf(
            pdf_dir / "dense.pdf",
            "turbine turbine turbine turbine turbine design",
        )
        # Sparse page: one 'turbine' in longer text
        make_pdf(
            pdf_dir / "sparse_mention.pdf",
            "This document covers many topics including a single mention of "
            "turbine among other long paragraphs about unrelated subjects "
            "that dilute the term frequency significantly in the overall text.",
        )

        index_pdfs(str(pdf_dir))
        results = search_pdfs("turbine", limit=10)

        assert len(results) >= 2
        assert results[0]["file"] == "dense.pdf"
