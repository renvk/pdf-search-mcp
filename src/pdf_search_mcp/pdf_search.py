#!/usr/bin/env python3
"""PDF full-text search tool.

Incrementally indexes PDFs under a directory into a SQLite FTS5 database
for instant full-text search with snippet extraction. Tracks file mtime
and size to detect new, changed, and deleted PDFs on each sync.

Invariants:
    - The FTS5 'pages' table indexes only the content column; file,
      subfolder, and page are UNINDEXED so query terms never match
      filenames or page numbers (schema v2 — older indexes must be
      rebuilt with 'reindex').
    - Filenames and subfolders are stored NFC-normalized; lookups
      normalize their inputs the same way.
    - Each file is committed individually during indexing, so a crash
      mid-run keeps all completed files and the incremental sync resumes.
    - All public functions raise PdfSearchError for expected failures
      (missing index, bad input, unreadable PDF); sqlite3.OperationalError
      escapes search_pdfs only for invalid raw FTS5 queries.
"""

import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from hashlib import sha1
from pathlib import Path
from urllib.parse import quote as _url_quote

import fitz  # PyMuPDF

from .query import extract_terms, prepare_query

# CoreGraphics (macOS) renders sharper fonts than PyMuPDF's FreeType
# rasterizer, especially math fonts. The import costs ~0.12s (pyobjc),
# so it is deferred until the first render instead of slowing down
# every index/search/stats invocation.
# None = not yet resolved; resolved to True/False on first use.
_USE_COREGRAPHICS = None
_render_cg = None


def _use_coregraphics():
    """Resolve (once) whether the CoreGraphics renderer is available."""
    global _USE_COREGRAPHICS, _render_cg
    if _USE_COREGRAPHICS is None:
        _USE_COREGRAPHICS = False
        if sys.platform == "darwin":
            try:
                from .render_cg import render_page_coregraphics

                _render_cg = render_page_coregraphics
                _USE_COREGRAPHICS = True
            except ImportError:
                pass
    return _USE_COREGRAPHICS


# Database location: configurable via PDF_SEARCH_DB env var,
# defaults to ~/.local/share/pdf-search-mcp/pdf_index.db
_DEFAULT_DB_DIR = Path.home() / ".local" / "share" / "pdf-search-mcp"
DB_PATH = Path(os.environ.get("PDF_SEARCH_DB", _DEFAULT_DB_DIR / "pdf_index.db"))

# FTS5 column index for 'content' in the pages table (file=0, subfolder=1, page=2, content=3)
_CONTENT_COL = 3

# Sentinel markers wrapped around matches by highlight() for density
# counting. Control characters (STX/ETX) never occur in extracted PDF
# text, unlike the visible '>>>' snippet markers — a page that literally
# contains '>>>' (shell transcripts, quoted email) must not inflate its
# density score.
_HL_OPEN = "\x02"
_HL_CLOSE = "\x03"

# Weight for match density in combined ranking score. Density boosts
# concentrated matches but cannot override BM25 relevance.
_DENSITY_WEIGHT = 0.3

# Max pixel length for the rendered image's long edge. Auto-DPI targets this
# value so region crops fill the vision model's resolution budget without
# triggering downscaling. Default 1568 is tuned for Claude's vision pipeline.
_MAX_RENDER_EDGE_PX = 1568

# Cap for region auto-DPI. Region output is clipped to _MAX_RENDER_EDGE_PX,
# so higher DPI only adds detail within that pixel budget — the cap bounds
# render time for tiny crops.
_MAX_REGION_DPI = 2500


def _density_components(highlighted):
    """Compute match density components from FTS5 highlighted text.

    Args:
        highlighted: Full page text with _HL_OPEN/_HL_CLOSE around each
            matched token (from FTS5 highlight()).

    Returns:
        (count_density, clustering) tuple.
        - count_density: match count / len(highlighted), or 0.0 if empty.
        - clustering: how tightly grouped markers are (0.0–1.0). For 2+
          matches: 1.0 - (span / text_length). For 0–1 matches: 0.5.
    """
    positions = [m.start() for m in re.finditer(_HL_OPEN, highlighted)]
    match_count = len(positions)
    if not highlighted:
        return (0.0, 0.5)
    count_density = match_count / len(highlighted)
    if match_count >= 2:
        span = positions[-1] - positions[0]
        clustering = 1.0 - (span / len(highlighted))
    else:
        clustering = 0.5
    return (count_density, clustering)


class PdfSearchError(Exception):
    """Raised when a PDF search operation fails."""


@contextmanager
def _get_db(readonly=False):
    """Open and yield a SQLite database connection, closing it on exit."""
    if readonly:
        # Percent-encode the path: characters like '#' or '?' would
        # otherwise be parsed as URI fragment/query and open a different
        # file (in read-write mode, silently creating it).
        uri = f"file:{_url_quote(str(DB_PATH), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    except BaseException:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


def _schema_is_current(conn):
    """True when the pages table exists with the v2 (UNINDEXED) schema.

    Derived from the actual DDL in sqlite_master rather than a stored
    version number, so it cannot drift from the real table definition.
    Raises sqlite3.DatabaseError when the file is not a SQLite database.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'pages'"
    ).fetchone()
    return row is not None and "UNINDEXED" in row["sql"]


@contextmanager
def _open_index():
    """Open the index read-only, validating existence, integrity, and schema.

    Yields an open connection.

    Raises:
        PdfSearchError: If the DB file is missing, is not a SQLite
            database, or uses an outdated schema.
    """
    if not DB_PATH.exists():
        raise PdfSearchError("No index found. Run 'index' first.")
    with _get_db(readonly=True) as conn:
        try:
            current = _schema_is_current(conn)
        except sqlite3.DatabaseError as e:
            raise PdfSearchError(
                f"'{DB_PATH}' is not a valid search index ({e}). "
                "Check PDF_SEARCH_DB or run 'index' first."
            ) from e
        if not current:
            raise PdfSearchError(
                "Index uses an outdated schema. Run 'reindex' to rebuild it."
            )
        yield conn


def _ensure_schema(conn):
    """Create tables if they don't exist.

    Tables:
        meta — key/value pairs (pdf_dir, last_indexed).
        pages — FTS5 full-text index. Only content is searchable; file,
            subfolder, and page are UNINDEXED metadata so query terms
            cannot match filenames or page numbers.
        files — tracks indexed PDFs for incremental sync and filename
            resolution (file, subfolder, mtime, size).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
            file UNINDEXED, subfolder UNINDEXED, page UNINDEXED, content
        );
        CREATE TABLE IF NOT EXISTS files (
            file TEXT NOT NULL,
            subfolder TEXT NOT NULL,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            PRIMARY KEY (file, subfolder)
        );
    """)


def _resolve_pdf_dir(pdf_dir):
    """Resolve the PDF root directory: argument → env var → stored meta.

    Args:
        pdf_dir: Directory path or None. Empty/whitespace-only values are
            treated as unset — Path('').resolve() is the current working
            directory, which must never be indexed by accident.

    Returns:
        The directory as given (str or Path), not validated for existence —
        callers validate so reindex can delete the DB before validation.

    Raises:
        PdfSearchError: If no source provides a directory.
    """
    def _unset(value):
        return value is None or not str(value).strip()

    if _unset(pdf_dir):
        pdf_dir = os.environ.get("PDF_SEARCH_DIR")
    if _unset(pdf_dir) and DB_PATH.exists():
        try:
            with _get_db(readonly=True) as conn:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'pdf_dir'"
                ).fetchone()
                if row:
                    pdf_dir = row["value"]
        except sqlite3.DatabaseError:
            pass  # DB corrupt or missing meta table — fall through to error
    if _unset(pdf_dir):
        raise PdfSearchError(
            "No PDF directory specified. Set PDF_SEARCH_DIR environment variable or pass pdf_dir argument."
        )
    return pdf_dir


def _scan_pdf_dir(pdf_dir):
    """Walk pdf_dir and collect metadata for all indexable PDFs.

    Args:
        pdf_dir: Resolved Path to the PDF root directory.

    Returns:
        (results, errors) tuple. results is a list of
        (filepath, fname_nfc, subfolder_nfc, mtime, size) tuples; errors
        is a list of (filename, message) for files that could not be
        stat'ed (dangling symlinks, files deleted mid-scan) — one bad
        entry must not abort the whole sync.
        Skips directories starting with '_' and non-.pdf files.
    """
    results = []
    errors = []
    for root, _dirs, files in os.walk(pdf_dir):
        root_path = Path(root)
        rel = root_path.relative_to(pdf_dir)
        if any(p.startswith("_") for p in rel.parts):
            continue

        for fname in sorted(files):
            if not fname.lower().endswith(".pdf"):
                continue

            filepath = root_path / fname
            # macOS HFS+/APFS may return NFD names (ä = a + combining ̈);
            # normalize filename AND subfolder to NFC so DB lookups match
            # MCP client input regardless of on-disk normalization
            subfolder = unicodedata.normalize("NFC", str(rel)) if rel.parts else ""
            fname_nfc = unicodedata.normalize("NFC", fname)
            try:
                stat = filepath.stat()
            except OSError as e:
                errors.append((fname_nfc, f"cannot stat: {e}"))
                continue
            results.append((filepath, fname_nfc, subfolder, stat.st_mtime, stat.st_size))
    return results, errors


def _index_single_pdf(conn, filepath, fname_nfc, subfolder):
    """Extract text from one PDF and insert non-empty pages into the FTS5 index.

    Args:
        conn: Open SQLite connection.
        filepath: Full path to the PDF file on disk.
        fname_nfc: NFC-normalized filename for DB storage.
        subfolder: NFC-normalized relative subfolder path ('' for root).

    Returns:
        Number of pages inserted (pages with non-empty text).
    """
    pages_added = 0
    with fitz.open(str(filepath)) as doc:
        for page_num in range(len(doc)):
            text = doc[page_num].get_text()
            if text.strip():
                conn.execute(
                    "INSERT INTO pages (file, subfolder, page, content) VALUES (?, ?, ?, ?)",
                    (fname_nfc, subfolder, page_num + 1, text),
                )
                pages_added += 1
    return pages_added


def index_pdfs(pdf_dir=None):
    """Incrementally sync the index with the PDF directory on disk.

    On first run, indexes all PDFs. On subsequent runs, detects new, changed,
    and deleted files by comparing mtime and size against the files table.
    Each file is committed individually: a crash mid-run keeps all completed
    files, and the next run resumes where it stopped.

    Args:
        pdf_dir: Path to the PDF directory. Falls back to PDF_SEARCH_DIR env var,
            then to the pdf_dir stored in the existing index's meta table.

    Returns:
        Dict with keys: files_added, files_updated, files_deleted, files_unchanged,
        total_files, total_pages, elapsed, errors.

    Raises:
        PdfSearchError: If no directory specified, directory doesn't exist,
            or the existing index uses an outdated schema (run 'reindex').
    """
    pdf_dir = Path(_resolve_pdf_dir(pdf_dir)).resolve()
    if not pdf_dir.is_dir():
        raise PdfSearchError(f"'{pdf_dir}' is not a directory.")

    errors = []
    t0 = time.time()

    with _get_db() as conn:
        # Refuse to mix schemas: incremental writes into a v1 table would
        # leave filenames searchable for old rows but not new ones
        try:
            has_pages = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'pages'"
            ).fetchone()
        except sqlite3.DatabaseError as e:
            raise PdfSearchError(
                f"'{DB_PATH}' is not a valid search index ({e}). Check PDF_SEARCH_DB."
            ) from e
        if has_pages and not _schema_is_current(conn):
            raise PdfSearchError(
                "Index uses an outdated schema. Run 'reindex' to rebuild it."
            )
        _ensure_schema(conn)

        # Phase 1: scan disk
        disk_files, scan_errors = _scan_pdf_dir(pdf_dir)
        errors.extend(scan_errors)
        desired = {
            (f_nfc, sub): (fpath, mt, sz)
            for fpath, f_nfc, sub, mt, sz in disk_files
        }

        # Phase 2: load indexed set from files table
        rows = conn.execute("SELECT file, subfolder, mtime, size FROM files").fetchall()
        indexed = {
            (r["file"], r["subfolder"]): (r["mtime"], r["size"])
            for r in rows
        }

        # Phase 3: compute diff
        desired_keys = set(desired)
        indexed_keys = set(indexed)

        to_add = desired_keys - indexed_keys
        to_delete = indexed_keys - desired_keys
        to_update = set()
        for key in desired_keys & indexed_keys:
            disk_mtime, disk_size = desired[key][1], desired[key][2]
            db_mtime, db_size = indexed[key]
            if disk_mtime != db_mtime or disk_size != db_size:
                to_update.add(key)

        # Phase 4: apply changes
        # Remove file records for deleted files
        for fname_nfc, subfolder in to_delete:
            conn.execute(
                "DELETE FROM pages WHERE file = ? AND subfolder = ?",
                (fname_nfc, subfolder),
            )
            conn.execute(
                "DELETE FROM files WHERE file = ? AND subfolder = ?",
                (fname_nfc, subfolder),
            )
        conn.commit()

        # Re-index changed files. Commit per file; rollback on failure
        # undoes the delete and partial inserts, preserving old pages.
        for fname_nfc, subfolder in sorted(to_update):
            filepath, mtime, size = desired[(fname_nfc, subfolder)]
            try:
                conn.execute(
                    "DELETE FROM pages WHERE file = ? AND subfolder = ?",
                    (fname_nfc, subfolder),
                )
                conn.execute(
                    "DELETE FROM files WHERE file = ? AND subfolder = ?",
                    (fname_nfc, subfolder),
                )
                _index_single_pdf(conn, filepath, fname_nfc, subfolder)
                conn.execute(
                    "INSERT INTO files (file, subfolder, mtime, size) VALUES (?, ?, ?, ?)",
                    (fname_nfc, subfolder, mtime, size),
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                errors.append((fname_nfc, str(e)))

        # Index new files. Commit per file; rollback on failure removes
        # partial page inserts so reruns cannot accumulate duplicate rows.
        for fname_nfc, subfolder in sorted(to_add):
            filepath, mtime, size = desired[(fname_nfc, subfolder)]
            try:
                _index_single_pdf(conn, filepath, fname_nfc, subfolder)
                conn.execute(
                    "INSERT INTO files (file, subfolder, mtime, size) VALUES (?, ?, ?, ?)",
                    (fname_nfc, subfolder, mtime, size),
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                errors.append((fname_nfc, str(e)))

        # Phase 5: recount totals from actual data — no drift possible
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('pdf_dir', ?)",
            (str(pdf_dir),),
        )

    elapsed = time.time() - t0
    files_unchanged = len(desired_keys & indexed_keys) - len(to_update)

    return {
        "files_added": len(to_add),
        "files_updated": len(to_update),
        "files_deleted": len(to_delete),
        "files_unchanged": files_unchanged,
        "total_files": total_files,
        "total_pages": total_pages,
        "elapsed": elapsed,
        "errors": errors,
    }


def search_pdfs(query, limit=10):
    """Full-text search across all indexed pages with density re-ranking.

    This is the raw FTS5 primitive: the query string is passed to MATCH
    unmodified. For user-typed queries use search_with_relaxation(), or
    pass the string through prepare_query() first.

    Args:
        query: FTS5 MATCH query string (supports AND, OR, NEAR, phrases).
        limit: Maximum number of results to return (int >= 1).

    Returns:
        List of dicts with keys: file, subfolder, page, snippet.

    Raises:
        PdfSearchError: If no index exists or limit < 1.
        sqlite3.OperationalError: If the query is not valid FTS5 syntax.
    """
    if limit < 1:
        # A negative limit would reach SQL as 'LIMIT -n' (unlimited in
        # SQLite) and then rows[:limit] would silently drop results
        raise PdfSearchError("limit must be a positive integer.")

    with _open_index() as conn:
        cursor = conn.execute(
            f"""
            SELECT file, subfolder, page,
                   snippet(pages, {_CONTENT_COL}, '>>>', '<<<', '...', 40) AS snippet,
                   highlight(pages, {_CONTENT_COL}, '{_HL_OPEN}', '{_HL_CLOSE}') AS highlighted,
                   rank
            FROM pages
            WHERE pages MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit * 3),
        )
        rows = [dict(row) for row in cursor.fetchall()]

    if not rows:
        return []

    # Ranking inputs are computed into parallel lists; the returned dicts
    # carry only the public keys (file, subfolder, page, snippet)
    densities = []
    clusterings = []
    ranks = []
    for row in rows:
        cd, cl = _density_components(row.pop("highlighted"))
        densities.append(cd)
        clusterings.append(cl)
        ranks.append(row.pop("rank"))

    # Normalize BM25 ranks to [0, 1] (most negative = best = 1.0)
    min_rank, max_rank = min(ranks), max(ranks)
    rank_span = max_rank - min_rank

    # Normalize count_density to [0, 1]
    max_density = max(densities)

    def _score(i):
        bm25_norm = (max_rank - ranks[i]) / rank_span if rank_span else 1.0
        cd_norm = densities[i] / max_density if max_density else 1.0
        density_blend = 0.5 * cd_norm + 0.5 * clusterings[i]
        return bm25_norm + _DENSITY_WEIGHT * density_blend

    order = sorted(range(len(rows)), key=_score, reverse=True)
    return [rows[i] for i in order[:limit]]


def search_with_relaxation(query, limit=10):
    """Prepared search with automatic relaxation on zero results.

    Runs the query through prepare_query() (auto-quoting, German digraph
    expansion, NEAR canonicalization), then relaxes multi-term queries
    that match nothing:

    Phase 1 (3+ terms): count matches for each single-term drop and keep
    the variant with the most matches corpus-wide — the dropped term is
    the one least represented in the corpus. Counts are uncapped, so the
    choice is correct even when result lists would saturate the limit.

    Phase 2: OR all original terms. BM25 ranking naturally prioritises
    pages matching more terms.

    Structured queries (explicit AND/OR/NOT, NEAR, parentheses) are never
    relaxed.

    Args:
        query: Raw user query string.
        limit: Maximum number of results (int >= 1).

    Returns:
        (results, note) tuple. results is a list of dicts (see
        search_pdfs); note is '' for direct matches, otherwise a
        human-readable string explaining what was actually searched.

    Raises:
        PdfSearchError: If no index exists, the query has no searchable
            terms, or the index is locked by a concurrent rebuild.
    """
    prepared = prepare_query(query)
    if not prepared:
        raise PdfSearchError("Query contains no searchable terms.")

    results = _execute_search(prepared, limit)
    if results:
        return results, ""

    terms = extract_terms(query)
    if not terms or len(terms) < 2:
        return [], ""

    # Phase 1: single-term drops, chosen by uncapped match counts
    if len(terms) >= 3:
        best_count = 0
        best_idx = -1
        with _open_index() as conn:
            for i in range(len(terms)):
                subset = prepare_query(" ".join(terms[:i] + terms[i + 1 :]))
                if not subset:
                    continue
                try:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM pages WHERE pages MATCH ?", (subset,)
                    ).fetchone()[0]
                except sqlite3.OperationalError:
                    continue  # a single bad variant must not kill relaxation
                if count > best_count:
                    best_count = count
                    best_idx = i
        if best_idx >= 0:
            kept = terms[:best_idx] + terms[best_idx + 1 :]
            results = _execute_search(prepare_query(" ".join(kept)), limit)
            if results:
                return results, f"No matches for full query. Relaxed to: {' '.join(kept)}"

    # Phase 2: OR all terms
    results = _execute_search(prepare_query(" OR ".join(terms)), limit)
    if results:
        return results, "No matches for full query. Showing pages matching any term."

    return [], ""


def _execute_search(prepared_query, limit):
    """Run search_pdfs, converting sqlite errors to PdfSearchError.

    prepare_query() output is valid FTS5 by construction, so an
    OperationalError here is either a concurrent-writer lock or a
    sanitizer bug — both must surface as a clear error, never be
    silently converted to 'no results'.
    """
    try:
        return search_pdfs(prepared_query, limit)
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            raise PdfSearchError(
                "Index is locked by a running index/reindex. Try again shortly."
            ) from e
        raise PdfSearchError(f"Search failed for query '{prepared_query}': {e}") from e


def _resolve_pdf_path(filename, subfolder=None):
    """Resolve a PDF filename to its full path on disk via the index.

    Args:
        filename: PDF filename (e.g. 'EN_13445-3_2021.pdf').
        subfolder: Subfolder to disambiguate duplicate filenames.
            '' selects the root folder explicitly; None means
            'unspecified' and is an error when duplicates exist.

    Returns:
        Path object to the PDF file.

    Raises:
        PdfSearchError: If index missing, file not found, filename is
            ambiguous (duplicates in several subfolders), or file not
            on disk.
    """
    # Normalize to NFC to match index storage (macOS filenames are NFD)
    filename = unicodedata.normalize("NFC", filename)

    with _open_index() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'pdf_dir'").fetchone()
        if not row:
            raise PdfSearchError("pdf_dir not found in metadata. Reindex.")
        pdf_dir = Path(row["value"])

        # The files table has one row per indexed PDF (PRIMARY KEY lookup),
        # including text-less scanned PDFs that have no pages rows
        if subfolder is not None:
            subfolder = unicodedata.normalize("NFC", subfolder)
            rows = conn.execute(
                "SELECT subfolder FROM files WHERE file = ? AND subfolder = ?",
                (filename, subfolder),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT subfolder FROM files WHERE file = ? ORDER BY subfolder",
                (filename,),
            ).fetchall()

        if not rows:
            raise PdfSearchError(f"File '{filename}' not found in index.")
        if len(rows) > 1:
            candidates = ", ".join(f"'{r['subfolder']}'" for r in rows)
            raise PdfSearchError(
                f"Multiple files named '{filename}' exist (subfolders: {candidates}). "
                "Pass the subfolder shown in the search result ('' for the root folder)."
            )

        resolved_subfolder = rows[0]["subfolder"]

    # The DB stores NFC but the filesystem may use either normalization
    # (macOS volumes are typically NFD) — try both forms for both parts
    sub_forms = dict.fromkeys(
        [unicodedata.normalize("NFD", resolved_subfolder), resolved_subfolder]
    )
    name_forms = dict.fromkeys([unicodedata.normalize("NFD", filename), filename])
    for sub in sub_forms:
        for name in name_forms:
            filepath = pdf_dir / sub / name
            if filepath.exists():
                return filepath
    raise PdfSearchError(
        f"File not found on disk: {pdf_dir / resolved_subfolder / filename}"
    )


@contextmanager
def _open_doc(filepath):
    """Open a PDF with fitz, converting backend errors to PdfSearchError.

    fitz raises FileDataError (a RuntimeError subclass) or ValueError for
    corrupt/unreadable files; callers above the core layer only handle
    PdfSearchError.
    """
    try:
        doc = fitz.open(str(filepath))
    except (RuntimeError, ValueError) as e:
        raise PdfSearchError(f"Cannot open '{Path(filepath).name}': {e}") from e
    try:
        yield doc
    finally:
        doc.close()


def _validate_page(doc, page_num):
    """Raise PdfSearchError unless 1 <= page_num <= len(doc)."""
    if page_num < 1 or page_num > len(doc):
        raise PdfSearchError(f"Page {page_num} out of range (1-{len(doc)}).")


def read_pdf_page(filename, page_num, subfolder=None):
    """Read full page text for a specific PDF and page number.

    Args:
        filename: PDF filename (e.g. 'EN_13445-3_2021.pdf').
        page_num: 1-based page number.
        subfolder: Subfolder to disambiguate duplicate filenames
            ('' = root folder, None = unspecified).

    Returns:
        The full page text.

    Raises:
        PdfSearchError: If file not found, ambiguous, unreadable, or page
            out of range.
    """
    filepath = _resolve_pdf_path(filename, subfolder)

    with _open_doc(filepath) as doc:
        _validate_page(doc, page_num)
        return doc[page_num - 1].get_text()


def _compute_region_dpi(page_width_pt, page_height_pt, region, dpi_cap):
    """Compute DPI so the crop's long edge equals _MAX_RENDER_EDGE_PX.

    Finds the DPI at which the region's longer dimension renders to exactly
    _MAX_RENDER_EDGE_PX pixels, then caps at dpi_cap. This fills the vision
    model's resolution budget without triggering downscaling.

    Args:
        page_width_pt: Page width in PDF points (1 pt = 1/72 inch).
        page_height_pt: Page height in PDF points.
        region: [x1, y1, x2, y2] fractional coordinates, validated by the
            caller (x1 < x2, y1 < y2 — so the long edge is never zero).
        dpi_cap: Maximum DPI.

    Returns:
        Effective DPI (int), capped at dpi_cap.
    """
    x1, y1, x2, y2 = region
    crop_w_pt = (x2 - x1) * page_width_pt
    crop_h_pt = (y2 - y1) * page_height_pt
    long_edge_pt = max(crop_w_pt, crop_h_pt)
    # pixels = points * dpi / 72  →  dpi = target_px * 72 / points
    computed_dpi = int(_MAX_RENDER_EDGE_PX * 72.0 / long_edge_pt)
    return min(computed_dpi, dpi_cap)


def _validate_region(region):
    """Validate a fractional crop region, raising PdfSearchError if invalid.

    Inputs: region is any sequence; valid form is [x1, y1, x2, y2] with
    each value in 0.0–1.0, x1 < x2, and y1 < y2 (zero-area regions would
    divide by zero in auto-DPI and produce zero-size bitmaps).
    """
    if len(region) != 4:
        raise PdfSearchError("region must be [x1, y1, x2, y2] (4 floats, each 0.0–1.0).")
    if not all(0.0 <= v <= 1.0 for v in region):
        raise PdfSearchError("region values must be between 0.0 and 1.0.")
    x1, y1, x2, y2 = region
    if x1 >= x2 or y1 >= y2:
        raise PdfSearchError("Invalid region: x1 must be < x2 and y1 must be < y2.")


def _render_output_path(filepath, page_num, dpi, region):
    """Build the deterministic output path for a rendered page.

    The name is keyed by full source path (hashed — duplicate filenames in
    different subfolders must not collide), page, DPI, and region, so a
    repeated call can reuse the cached file. Lives in a 0o700 subdirectory
    of the temp dir, created on first use.
    """
    out_dir = Path(tempfile.gettempdir()) / "pdf-search-mcp"
    out_dir.mkdir(mode=0o700, exist_ok=True)
    safe_name = re.sub(r"[^\w\-.]", "_", filepath.name)
    path_tag = sha1(str(filepath).encode("utf-8")).hexdigest()[:8]
    if region is not None:
        r_tag = "_r" + "_".join(f"{v:.2f}" for v in region)
    else:
        r_tag = ""
    return out_dir / f"pdf_page_{safe_name}_{path_tag}_p{page_num}_d{dpi}{r_tag}.png"


def render_pdf_page(filename, page_num, dpi=140, subfolder=None, region=None):
    """Render a PDF page (or region) as a PNG image.

    Useful for pages with formulas, diagrams, or tables that don't
    extract well as text. When region is set, dpi is ignored and DPI
    auto-scales (capped at _MAX_REGION_DPI) to fill the target vision
    resolution for the cropped area.

    Args:
        filename: PDF filename (e.g. 'EN_13445-3_2021.pdf').
        page_num: 1-based page number.
        dpi: Resolution for full-page rendering (default 140, must be >= 1).
            Ignored when region is set.
        subfolder: Subfolder to disambiguate duplicate filenames
            ('' = root folder, None = unspecified).
        region: Optional [x1, y1, x2, y2] fractional crop box (0.0–1.0,
            top-left origin). When set, only this region is rendered at
            auto-calculated DPI.

    Returns:
        Path to the rendered PNG file.

    Raises:
        PdfSearchError: If file not found, ambiguous, unreadable, page out
            of range, dpi < 1, or region invalid.
    """
    if dpi < 1:
        raise PdfSearchError("dpi must be a positive integer.")
    if region is not None:
        _validate_region(region)

    filepath = _resolve_pdf_path(filename, subfolder)

    with _open_doc(filepath) as doc:
        _validate_page(doc, page_num)
        page = doc[page_num - 1]
        page_rect = page.rect

        effective_dpi = dpi
        if region is not None:
            effective_dpi = _compute_region_dpi(
                page_rect.width, page_rect.height, region, _MAX_REGION_DPI
            )

        out = _render_output_path(filepath, page_num, effective_dpi, region)
        # Reuse a previous render of the same page/DPI/region unless the
        # source PDF changed since — rendering is the expensive step
        if out.exists() and out.stat().st_mtime > filepath.stat().st_mtime:
            return out

        if _use_coregraphics():
            cg_clip = None
            if region is not None:
                x1, y1, x2, y2 = region
                # Convert fractional top-left coords to CG bottom-left point
                # coords. page_rect is the rotation-applied CropBox — the CG
                # renderer sizes its bitmap from the same box, so these
                # coordinates land on the same content.
                cg_clip = (
                    x1 * page_rect.width,
                    (1.0 - y2) * page_rect.height,
                    (x2 - x1) * page_rect.width,
                    (y2 - y1) * page_rect.height,
                )
            try:
                png_bytes = _render_cg(
                    str(filepath), page_num, dpi=effective_dpi, clip_rect=cg_clip
                )
            except ValueError as e:
                raise PdfSearchError(
                    f"Render failed for '{filename}' p.{page_num}: {e}"
                ) from e
            out.write_bytes(png_bytes)
        else:
            if region is not None:
                x1, y1, x2, y2 = region
                clip = fitz.Rect(
                    x1 * page_rect.width,
                    y1 * page_rect.height,
                    x2 * page_rect.width,
                    y2 * page_rect.height,
                )
                pix = page.get_pixmap(dpi=effective_dpi, clip=clip)
            else:
                pix = page.get_pixmap(dpi=effective_dpi)
            pix.save(str(out))

    return out


def index_stats():
    """Return index statistics.

    Counts come from the files table and pages table directly, not from
    cached meta values — they cannot go stale after an interrupted run.

    Returns:
        Dict with keys: total_files (int), total_pages (int), last_indexed,
        db_size_mb, subfolders, renderer.

    Raises:
        PdfSearchError: If no index exists or it is invalid/outdated.
    """
    with _open_index() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_indexed'"
        ).fetchone()
        last_indexed = row["value"] if row else "?"

        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

        subfolder_counts = {
            row["subfolder"]: row["cnt"]
            for row in conn.execute(
                "SELECT subfolder, COUNT(*) as cnt FROM files GROUP BY subfolder ORDER BY subfolder"
            )
        }

    db_size = DB_PATH.stat().st_size / (1024 * 1024)

    return {
        "total_files": total_files,
        "total_pages": total_pages,
        "last_indexed": last_indexed,
        "db_size_mb": f"{db_size:.1f}",
        "subfolders": subfolder_counts,
        "renderer": "CoreGraphics" if _use_coregraphics() else "PyMuPDF",
    }


def reindex_pdfs(pdf_dir=None):
    """Drop and rebuild the index.

    Resolves the PDF directory (argument → PDF_SEARCH_DIR → stored meta)
    before deleting the database, so the path survives even when neither
    pdf_dir nor PDF_SEARCH_DIR is set.

    Args:
        pdf_dir: Path to PDF directory. Falls back to PDF_SEARCH_DIR env var,
            then to the pdf_dir stored in the existing index's meta table.

    Returns:
        Dict from index_pdfs(): files_added, files_updated, files_deleted,
        files_unchanged, total_files, total_pages, elapsed, errors.

    Raises:
        PdfSearchError: If no PDF directory can be determined. The DB is
            deleted even if the resolved directory turns out not to exist
            (reindex is destructive by contract — no stale index survives).
    """
    pdf_dir = _resolve_pdf_dir(pdf_dir)

    if DB_PATH.exists():
        DB_PATH.unlink()
    return index_pdfs(pdf_dir)


def _cli():
    """Command-line interface."""
    if len(sys.argv) < 2:
        print("Usage: python -m pdf_search_mcp.pdf_search <command> [args]")
        print("Commands:")
        print("  index  [pdf_dir]          Build the search index")
        print("  search <query> [limit]    Search indexed PDFs")
        print("  read   <file> <page> [sub] Read full page text")
        print("  stats                     Show index statistics")
        print("  reindex [pdf_dir]         Drop and rebuild index")
        print()
        print("Environment variables:")
        print("  PDF_SEARCH_DIR  Path to PDF directory (required for first index, remembered after)")
        print("  PDF_SEARCH_DB   Path to database file (default: ~/.local/share/pdf-search-mcp/pdf_index.db)")
        sys.exit(1)

    cmd = sys.argv[1]

    def _int_arg(pos, name, default=None):
        """Parse an integer CLI argument, exiting with a clear message."""
        if len(sys.argv) <= pos:
            return default
        try:
            return int(sys.argv[pos])
        except ValueError:
            print(f"Error: {name} must be an integer, got '{sys.argv[pos]}'", file=sys.stderr)
            sys.exit(1)

    try:
        if cmd == "index":
            pdf_dir = sys.argv[2] if len(sys.argv) > 2 else None
            result = index_pdfs(pdf_dir)
            print(
                f"Sync complete in {result['elapsed']:.1f}s: "
                f"+{result['files_added']} added, "
                f"~{result['files_updated']} updated, "
                f"-{result['files_deleted']} deleted, "
                f"={result['files_unchanged']} unchanged"
            )
            print(f"Total: {result['total_files']} files, {result['total_pages']} pages")
            if result["errors"]:
                print(f"  {len(result['errors'])} errors:")
                for fname, err in result["errors"][:10]:
                    print(f"    {fname}: {err}")

        elif cmd == "search":
            if len(sys.argv) < 3:
                print("Usage: python -m pdf_search_mcp.pdf_search search <query> [limit]")
                sys.exit(1)
            limit = _int_arg(3, "limit", default=10)
            results, note = search_with_relaxation(sys.argv[2], limit)
            if note:
                print(note)
            if not results:
                print("No results found.")
            for i, r in enumerate(results, 1):
                print(f"\n--- Result {i} ---")
                print(f"  File:      {r['file']}")
                print(f"  Subfolder: {r['subfolder']}")
                print(f"  Page:      {r['page']}")
                print(f"  Snippet:   {r['snippet']}")

        elif cmd == "read":
            if len(sys.argv) < 4:
                print("Usage: python -m pdf_search_mcp.pdf_search read <filename> <page> [subfolder]")
                sys.exit(1)
            filename = sys.argv[2]
            page_num = _int_arg(3, "page")
            subfolder = sys.argv[4] if len(sys.argv) > 4 else None
            text = read_pdf_page(filename, page_num, subfolder=subfolder)
            print(text)

        elif cmd == "stats":
            info = index_stats()
            print("Index stats:")
            print(f"  Files:        {info['total_files']}")
            print(f"  Pages:        {info['total_pages']}")
            print(f"  Last indexed: {info['last_indexed']}")
            print(f"  DB size:      {info['db_size_mb']} MB")
            print(f"  Subfolders:")
            for name, cnt in info["subfolders"].items():
                print(f"    {name or '(root)':30s} {cnt:4d} files")

        elif cmd == "reindex":
            pdf_dir = sys.argv[2] if len(sys.argv) > 2 else None
            result = reindex_pdfs(pdf_dir)
            print(f"Indexed {result['total_files']} files, {result['total_pages']} pages in {result['elapsed']:.1f}s")

        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)

    except PdfSearchError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
