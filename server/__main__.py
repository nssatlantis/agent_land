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
    from server import middleware as _middleware  # local: trusted-proxy summary

    logutil.log(
        "startup",
        db=db.DB_PATH,
        host=_host,
        port=_port,
        trusted_proxy_hosts=[str(h) for h in _middleware._trusted_proxy_hosts()],
        self_lan_ip=_middleware._SELF_LAN_IP,
    )
    uvicorn.run(
        app,
        host=_host,
        port=_port,
        timeout_keep_alive=config.HTTP_KEEPALIVE_TIMEOUT_SECONDS,
        timeout_graceful_shutdown=config.GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
