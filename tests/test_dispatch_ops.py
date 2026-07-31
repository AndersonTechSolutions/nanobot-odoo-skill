"""Tests for the model-level dispatch surfaces.

``get_inbox_data``, ``get_dispatch_board``, ``dispatch_assign`` and
``dispatch_unassign`` are all ``@api.model`` methods. Odoo forwards every
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
from odoo_skill.models.messaging import MessagingOps  # noqa: E402


def _args_of(mock_client, idx=0):
    """The (model, method, args_vector) of the idx-th execute_kw call."""
    call = mock_client._models.execute_kw.call_args_list[idx][0]
    return call[3], call[4], call[5]


@pytest.fixture()
def fsm(mock_client):
    ops = FieldServiceOps(mock_client)
    ops._available = True
    return ops


@pytest.fixture()
def msg(mock_client):
    ops = MessagingOps(mock_client)
    ops._available = True
    return ops


# ── Inbox ────────────────────────────────────────────────────────────


class TestInbox:

    def test_sends_no_ids_list(self, msg, mock_client):
        mock_client._models.execute_kw.return_value = {
            "conversations": [], "counts": {}, "agents": [], "canned": [],
        }
        msg.inbox(view="unassigned")
        model, method, args = _args_of(mock_client)
        assert (model, method) == ("atech.conversation", "get_inbox_data")
        assert args == ["unassigned", ""], (
            f"expected the declared positionals only, got {args!r}"
        )

    def test_search_is_forwarded(self, msg, mock_client):
        mock_client._models.execute_kw.return_value = {"conversations": []}
        msg.inbox(search="dell latitude")
        _, _, args = _args_of(mock_client)
        assert args == ["open", "dell latitude"]

    def test_summary_counts_the_visible_lane(self, msg, mock_client):
        mock_client._models.execute_kw.return_value = {
            "conversations": [{"id": 1}, {"id": 2}],
            "counts": {"open": 9, "unassigned": 4, "pending": 1},
            "agents": [], "canned": [],
        }
        out = msg.inbox()
        assert "2 conversation(s) shown" in out["summary"]
        assert "9 open" in out["summary"]
        assert out["counts"]["unassigned"] == 4

    def test_rejects_unknown_view(self, msg, mock_client):
        with pytest.raises(ValueError, match="view must be one of"):
            msg.inbox(view="archived")
        mock_client._models.execute_kw.assert_not_called()


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
        """XML-RPC cannot marshal None — it must go over as False."""
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
            True,
            [{"id": 5, "name": "Job", "planned_date_begin": False}],
        ]
        out = fsm.unschedule_job(5)
        assert out["status"] == "unscheduled"
        assert out["changed_anything"] is True

    def test_still_scheduled_is_reported_as_refused(self, fsm, mock_client):
        """The True return is worthless if the dates never cleared."""
        mock_client._models.execute_kw.side_effect = [
            True,
            [{"id": 5, "name": "Job",
              "planned_date_begin": "2026-08-03 13:00:00"}],
        ]
        out = fsm.unschedule_job(5)
        assert out["status"] == "refused"
        assert out["changed_anything"] is False
