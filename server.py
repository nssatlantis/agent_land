"""Compatibility shim — `python server.py` still runs the server (package is `server/`).

The real implementation lives in `server/` (see `server/__init__.py` facade,
`server/_mcp.py`, `server/_app.py`, `server/tools/*`). This file is kept
so `python server.py` and `pyproject.toml:files = ["server.py", "server"]`
keep working and `mypy`/`ruff` still see the old entrypoint.
"""

from server.__main__ import main

if __name__ == "__main__":
    main()
