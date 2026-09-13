"""server.ci_runner — sandboxed CI execution (slot pool, trees, runs).

Split package (moved verbatim from server/ci_runner.py): _slots holds the
slot pool, CPU fair-share and in-flight registry; _trees the runner trees
and git prepare paths; _sandbox the image build and process execution;
_runs the gating and run orchestration. This facade re-exports every name
so existing importers (server/tools/repo, server/poller, server/admin/_ci,
tests) keep working unchanged — including tests that monkeypatch module
attributes (which now target the owning submodule, e.g.
``server.ci_runner._sandbox._ensure_image``) and transitive module uses
(``ci_runner.subprocess``, ``ci_runner.github``, ...), which resolve to the
same objects.
"""

import hashlib  # noqa: F401
import os  # noqa: F401
import queue  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import signal  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401

import config  # noqa: F401
import db  # noqa: F401
import events  # noqa: F401
import github  # noqa: F401

from ._runs import (  # noqa: F401
    _CHECKS,
    _ENV_KEEP,
    _child_env,
    _ci_detail_with_output,
    _gate,
    _inflight_claim,
    _inflight_occupied,
    _inflight_release,
    _inflight_snapshot,
    ci_run_status,
    ledger_kind_for,
    run_branch_ci_for_poller,
    run_checks,
    run_checks_with_deadline,
    run_heartbeat_bench,
)
from ._sandbox import (  # noqa: F401
    _STATIC_SUMMARY_RE,
    _TRAVERSABLE_CACHE,
    _TRAVERSABLE_LOCK,
    _digest,
    _docker_available,
    _drain,
    _ensure_image,
    _ensure_tree_traversable,
    _execute,
    _image_tag,
    _kill_tree,
    _parse_static_summary,
    _parse_summary,
    _prune_stale_images,
    _requirements_at,
    _requirements_dev_at,
    _sandbox_argv,
    _stop_sandbox,
)
from ._slots import (  # noqa: F401
    _ACTIVE,
    _ACTIVE_CPUS,
    _ACTIVE_LOCK,
    _BUSY_LEGACY_MSG,
    _CI_LOCK,
    _CI_QUEUE,
    _CI_SLOTS,
    _INFLIGHT,
    _INFLIGHT_LOCK,
    _RUN_LOCK,
    _busy_msg,
    _ci_acquire_slot,
    _ci_ensure_pool,
    _ci_queue_depth,
    _ci_release_slot,
    _cpus_from_argv,
    _deregister_active,
    _effective_cpus,
    _host_cpus,
    _register_active,
    _throttle_active,
    ci_status_snapshot,
)
from ._trees import (  # noqa: F401
    _ORIG_RUNNER_DIR,
    _apply_local_changes,
    _br_dir,
    _ensure_clone,
    _git,
    _local_seed_available,
    _prepare_br_tree,
    _prepare_local_tree,
    _prepare_named_tree,
    _prepare_pr_tree,
    _prepare_tree,
    _refresh_main,
    _runner_dir,
    _runner_dir_for_slot,
    _runner_dir_impl,
    _sweep_idle_br_trees,
    _sweep_idle_named_trees,
    _try_clone_from_local,
    _validate_tree_name,
    evict_br_tree,
    forget_named_tree,
    list_br_trees,
    list_named_trees,
)
