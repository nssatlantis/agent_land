"""server.__main__ — python -m server entry, extracted from server.py."""

from __future__ import annotations

import sys

import uvicorn

import config
import db
import logutil
from server._app import _host, _port, app


def main() -> None:
    logutil.configure_logging()
    db.init_db()
    print(db.database_location_note(), file=sys.stderr)
    logutil.log("startup", db=db.DB_PATH, host=_host, port=_port)
    uvicorn.run(
        app, host=_host, port=_port,
        timeout_keep_alive=config.HTTP_KEEPALIVE_TIMEOUT_SECONDS,
    )


if __name__ == "__main__":
    main()
