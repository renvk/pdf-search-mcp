#!/usr/bin/env python3
"""PDF full-text search tool.

Pre-indexes all PDFs under a directory into a SQLite FTS5 database
for instant full-text search with snippet extraction.
"""

import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import fitz  # PyMuPDF

# Database location: configurable via PDF_SEARCH_DB env var,
# defaults to ~/.local/share/pdf-search-mcp/pdf_index.db
_DEFAULT_DB_DIR = Path.home() / ".local" / "share" / "pdf-search-mcp"
DB_PATH = Path(os.environ.get("PDF_SEARCH_DB", _DEFAULT_DB_DIR / "pdf_index.db"))

# FTS5 column index for 'content' in the pages table (file=0, subfolder=1, page=2, content=3)
_CONTENT_COL = 3


class PdfSearchError(Exception):
    """Raised when a PDF search operation fails."""


@contextmanager
def _get_db(readonly=False):
    """Open and yield a SQLite database connection, closing it on exit."""
    if readonly:
        uri = f"file:{DB_PATH}?mode=ro"
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


def _ensure_schema(conn):
    """Create tables if they don't exist.

    Tables:
        meta — key/value pairs (pdf_dir, total_files, total_pages, last_indexed).
        pages — FTS5 full-text index (file, subfolder, page, content).
        files — tracks indexed PDFs for incremental sync (file, subfolder, mtime, size).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
            file, subfolder, page, content
        );
        CREATE TABLE IF NOT EXISTS files (
            file TEXT NOT NULL,
            subfolder TEXT NOT NULL,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            PRIMARY KEY (file, subfolder)
        );
    """)


def _scan_pdf_dir(pdf_dir):
    """Walk pdf_dir and collect metadata for all indexable PDFs.

    Args:
        pdf_dir: Resolved Path to the PDF root directory.

    Returns:
        List of (filepath, fname_nfc, subfolder, mtime, size) tuples.
        Skips directories starting with '_' and non-.pdf files.
    """
    results = []
    for root, _dirs, files in os.walk(pdf_dir):
        root_path = Path(root)
        rel = root_path.relative_to(pdf_dir)
        if any(p.startswith("_") for p in rel.parts):
            continue

        for fname in sorted(files):
            if not fname.lower().endswith(".pdf"):
                continue

            filepath = root_path / fname
            subfolder = str(rel) if rel.parts else ""
            # macOS HFS+/APFS returns NFD filenames (ä = a + combining ̈);
            # normalize to NFC so DB lookups match MCP client input
            fname_nfc = unicodedata.normalize("NFC", fname)
            stat = filepath.stat()
            results.append((filepath, fname_nfc, subfolder, stat.st_mtime, stat.st_size))
    return results


def _index_single_pdf(conn, filepath, fname_nfc, subfolder):
    """Extract text from one PDF and insert non-empty pages into the FTS5 index.

    Args:
        conn: Open SQLite connection.
        filepath: Full path to the PDF file on disk.
        fname_nfc: NFC-normalized filename for DB storage.
        subfolder: Relative subfolder path (empty string for root).

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

    Args:
        pdf_dir: Path to the PDF directory. Can also be set via PDF_SEARCH_DIR env var.

    Returns:
        Dict with keys: files_added, files_updated, files_deleted, files_unchanged,
        total_files, total_pages, elapsed, errors.

    Raises:
        PdfSearchError: If no directory specified or directory doesn't exist.
    """
    if pdf_dir is None:
        pdf_dir = os.environ.get("PDF_SEARCH_DIR")
    if pdf_dir is None:
        raise PdfSearchError(
            "No PDF directory specified. Set PDF_SEARCH_DIR environment variable or pass pdf_dir argument."
        )

    pdf_dir = Path(pdf_dir).resolve()
    if not pdf_dir.is_dir():
        raise PdfSearchError(f"'{pdf_dir}' is not a directory.")

    errors = []
    t0 = time.time()

    with _get_db() as conn:
        _ensure_schema(conn)

        # Phase 1: scan disk
        disk_files = _scan_pdf_dir(pdf_dir)
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
        # Remove pages and file records for deleted/changed files
        for fname_nfc, subfolder in to_delete | to_update:
            conn.execute(
                "DELETE FROM pages WHERE file = ? AND subfolder = ?",
                (fname_nfc, subfolder),
            )
            conn.execute(
                "DELETE FROM files WHERE file = ? AND subfolder = ?",
                (fname_nfc, subfolder),
            )

        # Index new/changed files
        for fname_nfc, subfolder in to_add | to_update:
            filepath, mtime, size = desired[(fname_nfc, subfolder)]
            try:
                _index_single_pdf(conn, filepath, fname_nfc, subfolder)
                conn.execute(
                    "INSERT INTO files (file, subfolder, mtime, size) VALUES (?, ?, ?, ?)",
                    (fname_nfc, subfolder, mtime, size),
                )
            except Exception as e:
                errors.append((fname_nfc, str(e)))

        # Phase 5: recount totals from actual data — no drift possible
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('total_files', ?)",
            (str(total_files),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('total_pages', ?)",
            (str(total_pages),),
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
    """Full-text search across all indexed pages.

    Args:
        query: FTS5 MATCH query string (supports AND, OR, NEAR, phrases).
        limit: Maximum number of results to return.

    Returns:
        List of dicts with keys: file, subfolder, page, snippet.

    Raises:
        PdfSearchError: If no index exists.
    """
    if not DB_PATH.exists():
        raise PdfSearchError("No index found. Run 'index' first.")

    with _get_db(readonly=True) as conn:
        cursor = conn.execute(
            f"""
            SELECT file, subfolder, page,
                   snippet(pages, {_CONTENT_COL}, '>>>', '<<<', '...', 40) AS snippet
            FROM pages
            WHERE pages MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def _resolve_pdf_path(filename, subfolder=None):
    """Resolve a PDF filename to its full path on disk via the index.

    Args:
        filename: PDF filename (e.g. 'EN_13445-3_2021.pdf').
        subfolder: Optional subfolder to disambiguate duplicate filenames.

    Returns:
        Path object to the PDF file.

    Raises:
        PdfSearchError: If index missing, file not found, or file not on disk.
    """
    if not DB_PATH.exists():
        raise PdfSearchError("No index found. Run 'index' first.")

    # Normalize to NFC to match index storage (macOS filenames are NFD)
    filename = unicodedata.normalize("NFC", filename)

    with _get_db(readonly=True) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'pdf_dir'").fetchone()
        if not row:
            raise PdfSearchError("pdf_dir not found in metadata. Reindex.")
        pdf_dir = Path(row["value"])

        if subfolder is not None:
            row = conn.execute(
                "SELECT DISTINCT subfolder FROM pages WHERE file = ? AND subfolder = ? LIMIT 1",
                (filename, subfolder),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT DISTINCT subfolder FROM pages WHERE file = ? LIMIT 1",
                (filename,),
            ).fetchone()

        if not row:
            raise PdfSearchError(f"File '{filename}' not found in index.")

        resolved_subfolder = row["subfolder"]

    # Use NFD for disk lookup since macOS stores filenames in NFD
    filename_nfd = unicodedata.normalize("NFD", filename)
    filepath = pdf_dir / resolved_subfolder / filename_nfd
    if not filepath.exists():
        # Fallback: try NFC in case the filesystem isn't macOS
        filepath = pdf_dir / resolved_subfolder / filename
    if not filepath.exists():
        raise PdfSearchError(f"File not found on disk: {filepath}")

    return filepath


def read_pdf_page(filename, page_num, subfolder=None):
    """Read full page text for a specific PDF and page number.

    Args:
        filename: PDF filename (e.g. 'EN_13445-3_2021.pdf').
        page_num: 1-based page number.
        subfolder: Optional subfolder to disambiguate duplicate filenames.

    Returns:
        The full page text.

    Raises:
        PdfSearchError: If file not found or page out of range.
    """
    filepath = _resolve_pdf_path(filename, subfolder)

    with fitz.open(str(filepath)) as doc:
        if page_num < 1 or page_num > len(doc):
            raise PdfSearchError(f"Page {page_num} out of range (1-{len(doc)}).")
        return doc[page_num - 1].get_text()


def render_pdf_page(filename, page_num, dpi=150, subfolder=None):
    """Render a PDF page as a PNG image.

    Useful for pages with formulas, diagrams, or tables that don't
    extract well as text.

    Args:
        filename: PDF filename (e.g. 'EN_13445-3_2021.pdf').
        page_num: 1-based page number.
        dpi: Resolution for rendering (default 150).
        subfolder: Optional subfolder to disambiguate duplicate filenames.

    Returns:
        Path to the rendered PNG file.

    Raises:
        PdfSearchError: If file not found or page out of range.
    """
    filepath = _resolve_pdf_path(filename, subfolder)

    with fitz.open(str(filepath)) as doc:
        if page_num < 1 or page_num > len(doc):
            raise PdfSearchError(f"Page {page_num} out of range (1-{len(doc)}).")
        pix = doc[page_num - 1].get_pixmap(dpi=dpi)

    safe_name = re.sub(r'[^\w\-.]', '_', filename)
    out = Path(tempfile.gettempdir()) / f"pdf_page_{safe_name}_p{page_num}.png"
    pix.save(str(out))
    return out


def index_stats():
    """Return index statistics.

    Returns:
        Dict with keys: total_files, total_pages, last_indexed, db_size_mb, subfolders.

    Raises:
        PdfSearchError: If no index exists.
    """
    if not DB_PATH.exists():
        raise PdfSearchError("No index found. Run 'index' first.")

    with _get_db(readonly=True) as conn:
        meta = {}
        for row in conn.execute("SELECT key, value FROM meta"):
            meta[row["key"]] = row["value"]

        subfolder_counts = {
            row["subfolder"]: row["cnt"]
            for row in conn.execute(
                "SELECT subfolder, COUNT(DISTINCT file) as cnt FROM pages GROUP BY subfolder ORDER BY subfolder"
            )
        }

    db_size = DB_PATH.stat().st_size / (1024 * 1024)

    return {
        "total_files": meta.get("total_files", "?"),
        "total_pages": meta.get("total_pages", "?"),
        "last_indexed": meta.get("last_indexed", "?"),
        "db_size_mb": f"{db_size:.1f}",
        "subfolders": subfolder_counts,
    }


def reindex_pdfs(pdf_dir=None):
    """Drop and rebuild the index.

    Args:
        pdf_dir: Path to PDF directory.

    Returns:
        Dict from index_pdfs(): files_added, files_updated, files_deleted,
        files_unchanged, total_files, total_pages, elapsed, errors.
    """
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
        print("  PDF_SEARCH_DIR  Path to PDF directory (used by index/reindex)")
        print("  PDF_SEARCH_DB   Path to database file (default: ~/.local/share/pdf-search-mcp/pdf_index.db)")
        sys.exit(1)

    cmd = sys.argv[1]

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
            from .query import prepare_query

            query = prepare_query(sys.argv[2])
            limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10
            results = search_pdfs(query, limit)
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
            page_num = int(sys.argv[3])
            subfolder = sys.argv[4] if len(sys.argv) > 4 else None
            text = read_pdf_page(filename, page_num, subfolder=subfolder)
            print(text)

        elif cmd == "stats":
            info = index_stats()
            print(f"Index stats:")
            print(f"  Files:        {info['total_files']}")
            print(f"  Pages:        {info['total_pages']}")
            print(f"  Last indexed: {info['last_indexed']}")
            print(f"  DB size:      {info['db_size_mb']} MB")
            print(f"  Subfolders:")
            for name, cnt in info["subfolders"].items():
                print(f"    {name or '(root)':30s} {cnt:4d} files")

        elif cmd == "reindex":
            pdf_dir = sys.argv[2] if len(sys.argv) > 2 else None
            if DB_PATH.exists():
                print("Dropped existing index.")
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
