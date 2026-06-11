# Self-hosted deployment image: runs the MCP server over streamable
# HTTP so clients on a trusted network can connect without installing
# Python locally. Not needed for normal (stdio) use of the package.
#
# Mount points:
#   /pdfs  -- PDF collection (read-only mount is fine)
#   /data  -- persistent index database
#
# The entrypoint runs an incremental index sync on startup, then serves
# on port 8000 (see docker-entrypoint.sh). The server has no built-in
# authentication -- publish the port on trusted networks only.
FROM python:3.12-slim

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Created here so the container also starts without mounts (serves an
# empty index instead of failing on a missing directory).
RUN mkdir -p /pdfs /data

ENV PDF_SEARCH_DIR=/pdfs \
    PDF_SEARCH_DB=/data/pdf_index.db

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
