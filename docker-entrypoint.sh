#!/bin/sh
# Container entrypoint: sync the search index, then serve over HTTP.
#
# The sync is incremental (unchanged files are skipped), so restarts
# after the first index are fast and new PDFs dropped into the mounted
# directory are picked up on every container restart.
#
# Environment:
#   PDF_SEARCH_INDEX_ON_START  default 1; set 0 to skip the startup sync
#       (e.g. when the index is maintained by an external job).
set -e

if [ "${PDF_SEARCH_INDEX_ON_START:-1}" != "0" ]; then
    python -m pdf_search_mcp.pdf_search index
fi

# 0.0.0.0 is required inside a container -- the published port mapping,
# not the bind address, is the access-control boundary here.
exec pdf-search-mcp --transport http --host 0.0.0.0 --port 8000
