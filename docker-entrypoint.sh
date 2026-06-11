#!/bin/sh
# Container entrypoint: run an incremental index sync, then serve over
# HTTP.
#
# The index sync is incremental (unchanged files are skipped), so
# restarts after the first sync are fast and new PDFs dropped into the
# mounted directory are picked up on every container restart.
#
# Environment:
#   PDF_SEARCH_INDEX_ON_START  default 1; set 0 to skip the index sync
#       (e.g. when the index is maintained by an external job).
set -e

if [ "${PDF_SEARCH_INDEX_ON_START:-1}" != "0" ]; then
    # Fail fast on sync errors (a refused pre-0.3 database, unreadable
    # mounts) but say how to break a restart loop before exiting --
    # under `restart: unless-stopped` this message is the only clue.
    if ! python -m pdf_search_mcp.pdf_search index; then
        echo "Startup index sync failed (error above)." >&2
        echo "Fix the cause, or set PDF_SEARCH_INDEX_ON_START=0 to start" >&2
        echo "the server without syncing." >&2
        exit 1
    fi
fi

# 0.0.0.0 is required inside a container -- the published port mapping,
# not the bind address, is the access-control boundary here.
exec pdf-search-mcp --transport http --host 0.0.0.0 --port 8000
