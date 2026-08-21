"""Tests for the model-level dispatch surfaces.

``get_dispatch_board``, ``dispatch_assign`` and ``dispatch_unassign`` are all
``@api.model`` methods. Odoo forwards every
positional in the argument vector straight to a model-level method rather
than consuming a leading ids list, so these must be called with **no** ids
list — the failure mode that broke ``team-workload``.

The wire-shape assertions below are the point of this module: a mock that
only checks the returned payload cannot tell a correct call from one that
would raise "takes N positional arguments but N+1 were given" live.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.models.field_service import FieldServiceOps  # noqa: E402
from odoo_skill.models.pc_build import PcBuildOps  # noqa: E402


def _args_of(mock_client, idx=0):
    """The (model, method, args_vector) of the idx-th execute_kw call."""
    call = mock_client._models.execute_kw.call_args_list[idx][0]
    return call[3], call[4], call[5]


@pytest.fixture()
def fsm(mock_client):
    ops = FieldServiceOps(mock_client)
    ops._available = True
    ops._model_field_cache = set()
    return ops


# ── Dispatch board ───────────────────────────────────────────────────


class TestDispatchBoard:

    def test_sends_no_ids_list(self, fsm, mock_client):
        mock_client._models.execute_kw.return_value = {
            "days": [], "week_start": "2026-07-27", "technicians": [],
            "unscheduled": [], "scheduled": [],
        }
        fsm.dispatch_board(date_from="2026-07-27", days=5)
        model, method, args = _args_of(mock_client)
        assert (model, method) == ("project.task", "get_dispatch_board")
        assert args == ["2026-07-27", 5], (
            f"expected the declared positionals only, got {args!r}"
        )

    def test_omitted_date_passes_false_not_none(self, fsm, mock_client):
        """Odoo wants False for "no date", not None.

        The client sets ``allow_none=True``, so a None would marshal happily
        as XML-RPC ``<nil/>`` and then blow up inside ``fields.Date`` — the
        failure would surface remotely, not here.
        """
        mock_client._models.execute_kw.return_value = {}
        fsm.dispatch_board()
        _, _, args = _args_of(mock_client)
        assert args[0] is False
        assert None not in args

    def test_rejects_nonsense_window(self, fsm, mock_client):
        with pytest.raises(ValueError, match="days must be at least 1"):
            fsm.dispatch_board(days=0)
        mock_client._models.execute_kw.assert_not_called()


# ── Scheduling ───────────────────────────────────────────────────────


class TestScheduleJob:

    def test_refuses_without_confirm_and_issues_no_rpc(self, fsm, mock_client):
        """Scheduling notifies the customer, so it must not fire unasked."""
        out = fsm.schedule_job(task_id=5, date="2026-08-03", user_id=2)
        assert out["status"] == "needs_confirmation"
        assert out["changed_anything"] is False
        mock_client._models.execute_kw.assert_not_called()

    def test_confirmed_call_sends_no_ids_list(self, fsm, mock_client):
        mock_client._models.execute_kw.side_effect = [
            True,                                  # dispatch_assign
            [{"id": 5, "name": "Job"}],            # readback
        ]
        fsm.schedule_job(task_id=5, date="2026-08-03", user_id=2, confirm=True)
        model, method, args = _args_of(mock_client)
        assert (model, method) == ("project.task", "dispatch_assign")
        assert args == [5, 2, "2026-08-03"], (
            f"task_id must be a positional, not an ids list; got {args!r}"
        )

    def test_missing_technician_goes_over_as_false(self, fsm, mock_client):
        mock_client._models.execute_kw.side_effect = [True, [{"id": 5}]]
        fsm.schedule_job(task_id=5, date="2026-08-03", confirm=True)
        _, _, args = _args_of(mock_client)
        assert args[1] is False

    def test_bare_false_is_reported_as_refused(self, fsm, mock_client):
        """Odoo returns False when the task or user is not FSM-eligible.

        Nothing is written in that case, so it must not read as success.
        """
        mock_client._models.execute_kw.return_value = False
        out = fsm.schedule_job(task_id=5, date="2026-08-03", user_id=2,
                               confirm=True)
        assert out["status"] == "refused"
        assert out["changed_anything"] is False


class TestUnscheduleJob:

    def test_success_is_verified_by_readback(self, fsm, mock_client):
        """dispatch_unassign returns True unconditionally — trust the record."""
        mock_client._models.execute_kw.side_effect = [
            [{"id": 5, "name": "Job",
              "planned_date_begin": "2026-08-03 13:00:00"}],   # before
            True,                                              # the RPC
            [{"id": 5, "name": "Job", "planned_date_begin": False}],  # after
        ]
        out = fsm.unschedule_job(5)
        assert out["status"] == "unscheduled"
        assert out["changed_anything"] is True

    def test_sends_task_id_as_a_positional_not_an_ids_list(self, fsm, mock_client):
        mock_client._models.execute_kw.side_effect = [
            [{"id": 5, "planned_date_begin": "2026-08-03 13:00:00"}],
            True,
            [{"id": 5, "planned_date_begin": False}],
        ]
        fsm.unschedule_job(5)
        model, method, args = _args_of(mock_client, 1)
        assert (model, method) == ("project.task", "dispatch_unassign")
        assert args == [5], f"expected a bare positional, got {args!r}"

    def test_already_unscheduled_is_a_no_op_not_a_success(self, fsm, mock_client):
        """The end state is identical either way — only the prior state tells.

        Reporting changed_anything=True here would be a lie, and the RPC
        should not fire at all.
        """
        mock_client._models.execute_kw.return_value = [
            {"id": 5, "name": "Job", "planned_date_begin": False}
        ]
        out = fsm.unschedule_job(5)
        assert out["status"] == "no_change"
        assert out["changed_anything"] is False
        assert mock_client._models.execute_kw.call_count == 1

    def test_still_scheduled_is_reported_as_refused(self, fsm, mock_client):
        """The True return is worthless if the dates never cleared."""
        mock_client._models.execute_kw.side_effect = [
            [{"id": 5, "planned_date_begin": "2026-08-03 13:00:00"}],  # before
            True,
            [{"id": 5, "name": "Job",
              "planned_date_begin": "2026-08-03 13:00:00"}],
        ]
        out = fsm.unschedule_job(5)
        assert out["status"] == "refused"
        assert out["changed_anything"] is False


# ── Required-argument validation ─────────────────────────────────────


class TestNullArguments:
    """``allow_none=True`` means a None would reach Odoo, not fail locally."""

    def test_schedule_rejects_null_date(self, fsm, mock_client):
        with pytest.raises(ValueError, match="required"):
            fsm.schedule_job(task_id=5, date=None, confirm=True)
        mock_client._models.execute_kw.assert_not_called()

    def test_schedule_rejects_null_task_id(self, fsm, mock_client):
        with pytest.raises(ValueError, match="required"):
            fsm.schedule_job(task_id=None, date="2026-08-03", confirm=True)
        mock_client._models.execute_kw.assert_not_called()

    def test_unschedule_rejects_null_task_id(self, fsm, mock_client):
        with pytest.raises(ValueError, match="required"):
            fsm.unschedule_job(None)
        mock_client._models.execute_kw.assert_not_called()


# ── CLI exposure ─────────────────────────────────────────────────────


class TestModelHelperIsNotCliReachable:
    """``_call_model`` must stay private.

    The generic ``call`` command exposes every public callable and infers
    write-ness from the *requested* name. A public ``call_model`` would
    therefore be classified as a read, letting
    ``call field_service.call_model --args '{"method": "dispatch_assign", ...}'``
    perform a customer-notifying write with no --confirm, and reach arbitrary
    model-level methods on every namespace — bypassing ALLOWED_ACTIONS too.
    """

    def test_helper_is_private(self):
        from odoo_skill.models._base import BaseOps
        assert not hasattr(BaseOps, "call_model")
        assert hasattr(BaseOps, "_call_model")

    def test_no_ops_class_exposes_a_public_model_caller(self):
        """No subclass may re-expose it under a public name either."""
        from odoo_skill.models._base import BaseOps
        for cls in (FieldServiceOps, PcBuildOps):
            public = [
                m for m in dir(cls)
                if not m.startswith("_") and "call_model" in m
            ]
            assert public == [], f"{cls.__name__} exposes {public}"

    def test_write_classification(self):
        """odoo.py's resolver rejects anything starting with an underscore."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "odoo_cli", os.path.join(SKILL_DIR, "odoo.py"))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        assert not cli._op_writes("dispatch_board")
        assert cli._op_writes("schedule_job")
        assert cli._op_writes("unschedule_job")
