"""Tests for BaseOps — the safety layer under the custom-module ops classes.

Two behaviours matter most here:

* **Action allowlisting.** ``execute_kw`` will invoke any public method on a
  model, including ``unlink``. Button dispatch must refuse anything outside
  the class's ``ALLOWED_ACTIONS``.
* **Computed-field filtering.** Several custom modules expose flags as
  non-stored computed fields. Odoo does not reject a domain over one — it
  silently drops the clause and returns the *unfiltered* set. Those filters
  therefore run client-side, and must actually filter.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.models._base import (  # noqa: E402
    BaseOps,
    OdooActionNotAllowedError,
    OdooModuleNotInstalledError,
)
from odoo_skill.models.repair import RepairOps  # noqa: E402
from odoo_skill.models.itad import ITADOps, _within_sla  # noqa: E402
from odoo_skill.models.field_service import FieldServiceOps  # noqa: E402


class DummyOps(BaseOps):
    MODEL = "dummy.model"
    MODULE = "dummy_module"
    LIST_FIELDS = ["id", "name", "flag"]
    ALLOWED_ACTIONS = frozenset({"action_go"})


@pytest.fixture()
def dummy(mock_client):
    ops = DummyOps(mock_client)
    ops._available = True          # skip the fields_get probe
    ops._model_field_cache = set()  # schema unknown -> _fields() does not filter
    return ops


# ── Action allowlist ─────────────────────────────────────────────────


class TestActionAllowlist:

    def test_refuses_method_outside_allowlist(self, dummy):
        with pytest.raises(OdooActionNotAllowedError) as exc:
            dummy.run_action(1, "unlink")
        assert "unlink" in str(exc.value)
        assert "not permitted" in str(exc.value)

    def test_refuses_without_issuing_rpc(self, dummy, mock_client):
        with pytest.raises(OdooActionNotAllowedError):
            dummy.run_action(1, "unlink")
        mock_client._models.execute_kw.assert_not_called()

    def test_permits_allowlisted_method(self, dummy, mock_client):
        mock_client._models.execute_kw.side_effect = [
            True,                                        # the action
            [{"id": 1, "name": "x", "flag": False}],     # the readback
        ]
        out = dummy.run_action(1, "action_go")
        assert out["method"] == "action_go"
        assert out["record"]["id"] == 1

    def test_dispatch_sends_the_id_as_a_record_list(self, dummy, mock_client):
        """Button methods are record methods — the id goes in a leading list.

        The mirror of the ``get_workload_data`` bug: model-level methods take
        no ids list, record methods require one. Asserting only the return
        shape leaves the argument vector unchecked, which is how that bug
        survived a passing suite.
        """
        mock_client._models.execute_kw.side_effect = [
            True,
            [{"id": 7, "name": "x", "flag": False}],
        ]
        dummy.run_action(7, "action_go")

        # execute_kw(db, uid, key, model, method, args_list, kwargs_dict)
        call = mock_client._models.execute_kw.call_args_list[0][0]
        assert call[4] == "action_go"
        assert call[5] == [[7]], f"expected a leading ids list, got {call[5]!r}"

    def test_actions_lists_the_allowlist(self, dummy):
        assert dummy.actions() == ["action_go"]

    def test_repair_allowlist_excludes_destructive_methods(self, mock_client):
        ops = RepairOps(mock_client)
        assert "unlink" not in ops.ALLOWED_ACTIONS
        assert "write" not in ops.ALLOWED_ACTIONS
        assert "action_repair_start" in ops.ALLOWED_ACTIONS

    def test_itad_exposes_no_button_methods(self, mock_client):
        """ITAD state changes carry compliance weight — none are allowlisted."""
        assert ITADOps(mock_client).ALLOWED_ACTIONS == frozenset()


# ── Module availability guard ────────────────────────────────────────


class TestModuleGuard:

    def test_missing_model_raises_named_error(self, mock_client):
        ops = DummyOps(mock_client)
        ops._available = False
        with pytest.raises(OdooModuleNotInstalledError) as exc:
            ops.search()
        assert "dummy_module" in str(exc.value)

    def test_availability_is_cached(self, mock_client):
        ops = DummyOps(mock_client)
        mock_client._models.execute_kw.return_value = {"id": {"type": "integer"}}
        assert ops.available() is True
        assert ops.available() is True
        # fields_get is itself cached on the client, so one call at most
        assert mock_client._models.execute_kw.call_count <= 1


# ── Client-side filtering of non-stored computed fields ──────────────


class TestComputedFiltering:

    def test_search_computed_actually_filters(self, dummy, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 1, "name": "a", "flag": True},
            {"id": 2, "name": "b", "flag": False},
            {"id": 3, "name": "c", "flag": True},
        ]
        out = dummy.search_computed([], lambda r: r["flag"], limit=10,
                                    extra_fields=["flag"])
        assert [r["id"] for r in out] == [1, 3]

    def test_search_computed_respects_limit(self, dummy, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": i, "name": str(i), "flag": True} for i in range(10)
        ]
        out = dummy.search_computed([], lambda r: r["flag"], limit=3,
                                    extra_fields=["flag"])
        assert len(out) == 3

    def test_count_computed_counts_only_matches(self, dummy, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 1, "flag": True},
            {"id": 2, "flag": False},
            {"id": 3, "flag": True},
        ]
        assert dummy.count_computed([], lambda r: r["flag"],
                                    extra_fields=["flag"]) == 2

    def test_extra_fields_are_requested(self, dummy, mock_client):
        mock_client._models.execute_kw.return_value = []
        dummy.search_computed([], lambda r: True, limit=5,
                              extra_fields=["is_overdue"])
        # execute_kw(db, uid, key, model, method, args_list, kwargs_dict)
        kwargs = mock_client._models.execute_kw.call_args[0][6]
        assert "is_overdue" in kwargs["fields"]


# ── Domain correctness regressions ───────────────────────────────────


class TestDomainRegressions:
    """Guards against reintroducing filters on unsearchable fields."""

    def test_field_service_stays_scoped_to_fsm_tasks(self, mock_client):
        """Every FieldServiceOps query must be scoped to FSM tasks.

        ``is_fsm`` and ``project_id.is_fsm`` are equally valid here — the
        former is related to the latter and Odoo resolves either server-side.
        What matters is that *some* FSM scope is present, so the class can
        never fall back to returning every ``project.task``.
        """
        domain = FieldServiceOps(mock_client).BASE_DOMAIN
        assert "is_fsm" in str(domain)
        assert domain, "BASE_DOMAIN must not be empty"

    def test_within_sla_excludes_jobs_without_a_deadline(self):
        """sla_days_remaining is False when unset — not 'zero days left'."""
        assert _within_sla(False, 3) is False
        assert _within_sla(None, 3) is False
        assert _within_sla(2, 3) is True
        assert _within_sla(5, 3) is False

    def test_within_sla_tolerates_garbage(self):
        assert _within_sla("nonsense", 3) is False
