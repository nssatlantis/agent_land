"""db._core — DB infrastructure package (split verbatim from db/_core.py).

_errors holds ForumError, _paths the path constants, _time the timestamps,
_observe the sqlite observability state, _conn the connection handling,
_migrate the schema-migration primitives, _auth the token/karma gates,
_init the init_db orchestrator with the _boot_* migration phases.
This facade re-exports every name the old module exposed so all
existing importers (db/__init__, sibling modules, tests) keep working
unchanged.
"""

from __future__ import annotations

from ._auth import (  # noqa: F401
    _account_status_for,
    _humanize_interval,
    _require_active_agent,
    _require_agent_by_token,
    _require_agent_with_ent,
    active_citizens,
    require_active,
    require_active_agent,
    require_min_karma,
)
from ._conn import (  # noqa: F401
    _conn,
    _id_chunks,
    earliest_record_iso,
)
from ._errors import (  # noqa: F401
    ForumError,
)
from ._init import (  # noqa: F401
    init_db,
)
from ._observe import (  # noqa: F401
    _log_slow_block_if_needed,
    slow_block_stats,
    stats_refreshed_at,
)
from ._paths import (  # noqa: F401
    DATA_DIR,
    DB_PATH,
    REPLY_SEPARATOR,
    REPO_DIR,
    SCHEMA_PATH,
    _ensure_db_dir,
    database_location_note,
)
from ._time import (  # noqa: F401
    _now_iso,
    _parse_iso,
    _since_bound,
    now,
)
