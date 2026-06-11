"""Allow running as: python -m pdf_search_mcp"""

from .mcp_server import main

# Guarded so importing this module (test collection, pydoc, pkgutil
# walkers) does not start the blocking stdio server.
if __name__ == "__main__":
    main()
