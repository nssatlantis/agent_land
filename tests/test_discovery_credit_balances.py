"""Unit tests for server.tools.discovery._attach_credit_balances.

Rows built on _AGENT_LIST_SQL (list_agents / public_agent_detail /
public_agents_detail - the only inputs the helper sees via get_citizen_profiles)
already carry `credits_units` via the aggregated `cb` CTE. The helper must
reuse that value instead of re-running a redundant balances_for batch query per
profile (register finding #4801).
"""

import os
import sys
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server.tools.discovery import _attach_credit_balances  # noqa: E402


def test_reuses_existing_credits_units_without_requery():
    """When every row already carries credits_units, balances_for must NOT
    be called - it is a redundant batch (the common _AGENT_LIST_SQL path)."""
    rows = [
        {"agent_id": 1, "name": "a", "credits_units": 8},
        {"agent_id": 2, "name": "b", "credits_units": 9},
    ]
    with patch("db._credits.balances_for") as balances_for:
        out = _attach_credit_balances(rows)
        balances_for.assert_not_called()
    # values reused unchanged; formatted `credits` string still attached
    assert out[0]["credits_units"] == 8 and out[0]["credits"] == "0.4"
    assert out[1]["credits_units"] == 9 and out[1]["credits"] == "0.45"


def test_batches_only_ids_missing_credits_units():
    """A row that lacks credits_units is still filled, but only the missing
    ids are batched - never a re-batch of ids that already carry it."""
    rows = [
        {"agent_id": 1, "name": "a", "credits_units": 8},
        {"agent_id": 2, "name": "b"},
        {"agent_id": 3, "name": "c"},
    ]
    with patch("db._credits.balances_for") as balances_for:
        balances_for.return_value = {2: 9, 3: 10}
        out = _attach_credit_balances(rows)
        balances_for.assert_called_once_with([2, 3])
    assert out[0]["credits_units"] == 8 and out[0]["credits"] == "0.4"
    assert out[1]["credits_units"] == 9 and out[1]["credits"] == "0.45"
    assert out[2]["credits_units"] == 10 and out[2]["credits"] == "0.5"


def main():
    test_reuses_existing_credits_units_without_requery()
    test_batches_only_ids_missing_credits_units()
    print("credit-balance attach tests passed")


if __name__ == "__main__":
    main()
