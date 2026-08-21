"""
Field Service operations for ``atech_field_service`` (+ ``_syncro``).

FSM jobs are Odoo Enterprise ``project.task`` records flagged ``is_fsm``,
extended with scheduling, on-site photos (``atech.fsm.image``), customer
signature capture, SMS notifications, and a Syncro ticket link.

Source records (repair orders, RMAs, warranty claims, helpdesk tickets) all
mix in ``atech.fsm.source.mixin`` and expose ``action_schedule_fsm_job`` —
so scheduling a job *from* one of those is done through that record's own
ops class, not here. This class covers the jobs themselves.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "partner_id", "user_ids", "stage_id", "priority",
    "planned_date_begin", "date_deadline", "fsm_done", "project_id",
]
_DETAIL_FIELDS = _LIST_FIELDS + [
    "description", "partner_phone",
    "fsm_signed_by", "fsm_confirmation_sent", "fsm_reminder_sent",
    "sms_fsm_confirmation_sent", "sms_fsm_reminder_sent",
    "sms_fsm_completed_sent", "fsm_image_ids", "syncro_ticket_id",
    "tag_ids", "state",
]

_IMAGE_FIELDS = ["id", "display_name", "create_date"]


class FieldServiceOps(BaseOps):
    """Operations on field-service jobs (``project.task`` where ``is_fsm``)."""

    MODEL = "project.task"
    MODULE = "atech_field_service"
    IMAGE_MODEL = "atech.fsm.image"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "planned_date_begin asc"

    #: Every search in this class is scoped to FSM tasks.
    #:
    #: ``project.task.is_fsm`` is non-stored but *is* searchable (it is
    #: ``related=`` to ``project.project.is_fsm``, which Odoo rewrites the
    #: domain to), so ``["is_fsm", "=", True]`` would work equally well. This
    #: spells out the traversal to the stored source of truth explicitly —
    #: identical semantics, one less indirection to reason about.
    BASE_DOMAIN = [["project_id.is_fsm", "=", True]]

    ALLOWED_ACTIONS = frozenset({
        # job lifecycle
        "action_fsm_validate",
        "action_unschedule_task",
        # on-site
        "action_fsm_notify_on_way",
        "action_fsm_create_quotation",
        # technician timers
        "action_timer_start",
        "action_timer_pause",
        "action_timer_resume",
        "action_timer_stop",
        # labels
        "action_print_task_label_zpl",
    })

    def _scoped(self, domain: Optional[list] = None) -> list:
        """Prefix a domain with the ``is_fsm`` filter."""
        return list(self.BASE_DOMAIN) + list(domain or [])

    # ── Reads ────────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        domain: Optional[list] = None,
        limit: int = 50,
        offset: int = 0,
        order: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> list[dict]:
        """Search FSM jobs only — ``is_fsm`` is always applied."""
        return super().search(
            self._scoped(domain), limit=limit, offset=offset,
            order=order, fields=fields,
        )

    def count(self, domain: Optional[list] = None) -> int:  # type: ignore[override]
        """Count FSM jobs only."""
        return super().count(self._scoped(domain))

    def scheduled_jobs(self, limit: int = 50) -> list[dict]:
        """Jobs with a planned start that are not yet done."""
        return self.search(
            [["planned_date_begin", "!=", False], ["fsm_done", "=", False]],
            limit=limit,
        )

    def unscheduled_jobs(self, limit: int = 50) -> list[dict]:
        """Jobs still needing a slot on the dispatch board."""
        return self.search(
            [["planned_date_begin", "=", False], ["fsm_done", "=", False]],
            limit=limit,
        )

    def jobs_for_technician(self, user_id: int, limit: int = 50) -> list[dict]:
        """A technician's open jobs."""
        return self.search(
            [["user_ids", "in", [user_id]], ["fsm_done", "=", False]], limit=limit
        )

    # ── Dispatch board ───────────────────────────────────────────────

    def dispatch_board(
        self, date_from: Optional[str] = None, days: int = 7
    ) -> dict:
        """The dispatch board for a date window, as the drag-drop UI sees it.

        Wraps ``project.task.get_dispatch_board`` — one round-trip for the
        technician roster, day columns, the unscheduled backlog and every
        scheduled card, already timezone-resolved to the company's tz.

        Args:
            date_from: ``YYYY-MM-DD`` for the first column. Defaults to the
                Monday of the current week (the module's own default).
            days: Number of day columns.

        Returns:
            Dict with ``days``, ``week_start``, ``technicians``,
            ``unscheduled`` and ``scheduled``, plus a rendered ``summary``.

        Raises:
            OdooError: Wrapping Odoo's ``AccessError`` when the API user is
                not in ``industry_fsm.group_fsm_user``. The module enforces
                that group on every dispatch RPC.
        """
        if days < 1:
            raise ValueError(f"days must be at least 1, got {days!r}")
        # @api.model — no ids list (see BaseOps._call_model).
        data = self._call_model("get_dispatch_board", date_from or False, days)

        backlog = data.get("unscheduled", []) or []
        booked = data.get("scheduled", []) or []
        techs = data.get("technicians", []) or []
        return {
            "summary": (
                f"Dispatch board from {data.get('week_start')} ({days}d): "
                f"{len(booked)} scheduled across {len(techs)} technician(s), "
                f"{len(backlog)} unscheduled"
            ),
            **data,
        }

    def schedule_job(
        self,
        task_id: int,
        date: str,
        user_id: Optional[int] = None,
        confirm: bool = False,
    ) -> dict:
        """Place a job on the board for a technician and day.

        **This messages the customer.** The module calls
        ``_fsm_notify_scheduled()`` on success, confirming the appointment, so
        this is not a dry-run-able operation — hence *confirm*.

        Delivery is not guaranteed and Odoo does not report it back: that
        helper silently sends nothing when the customer has no email, the
        template is missing, or a confirmation already went out. The result
        therefore says "notification attempted", never "notified".

        Args:
            task_id: The FSM task to schedule.
            date: ``YYYY-MM-DD`` for the day column. The start hour and
                duration come from the module's own config parameters.
            user_id: Technician to assign. ``None`` drops the job on the
                "Unassigned" lane, clearing any existing assignee.
            confirm: Must be ``True`` to proceed.

        Returns:
            The updated job, or a ``needs_confirmation`` envelope.
        """
        if task_id is None or date is None:
            # The client enables allow_none=True, so a None here would be
            # marshalled as XML-RPC <nil/> and fail inside Odoo. Fail locally
            # with a usable message instead.
            raise ValueError(
                f"task_id and date are required, got task_id={task_id!r}, "
                f"date={date!r}"
            )
        if not confirm:
            return {
                "status": "needs_confirmation",
                "summary": (
                    f"Scheduling task {task_id} for {date} will notify the "
                    f"customer of the appointment. Re-run with confirm=True."
                ),
                "would_schedule": {
                    "task_id": task_id, "date": date, "user_id": user_id,
                },
                "changed_anything": False,
            }
        ok = self._call_model(
            "dispatch_assign", task_id, user_id or False, date
        )
        # The module returns a bare False when it refuses — the task is not an
        # FSM task, or the target user is not an FSM technician. Nothing is
        # written in that case, so surface it rather than reporting success.
        if not ok:
            return {
                "status": "refused",
                "summary": (
                    f"Odoo refused to schedule task {task_id}. It is either "
                    f"not an FSM task, or user {user_id} is not in the field "
                    f"service technician group."
                ),
                "changed_anything": False,
            }
        return {
            "status": "scheduled",
            "summary": f"Task {task_id} scheduled for {date}"
                       + (f" to user {user_id}" if user_id else " (unassigned)")
                       + "; customer notification attempted.",
            "changed_anything": True,
            "job": self.get(task_id),
        }

    def unschedule_job(self, task_id: int) -> dict:
        """Return a job to the unscheduled backlog, clearing its dates.

        Unlike :meth:`schedule_job` this sends no customer notification, so it
        needs no confirmation gate.

        ``dispatch_unassign`` returns ``True`` unconditionally — including
        when it declines to touch the record because the id is unknown or the
        task is not an FSM task. Its return value proves nothing, so the
        outcome is derived from the record itself.

        Reading the *prior* state matters as much as the result: a task that
        was already unscheduled ends in the same place as one this call
        actually moved, and reporting ``changed_anything`` for a no-op would
        be a lie.
        """
        if task_id is None:
            raise ValueError("task_id is required")
        before = self.get(task_id)
        if not before.get("planned_date_begin"):
            return {
                "status": "no_change",
                "summary": f"Task {task_id} was already unscheduled.",
                "changed_anything": False,
                "job": before,
            }

        self._call_model("dispatch_unassign", task_id)
        job = self.get(task_id)
        if job.get("planned_date_begin"):
            return {
                "status": "refused",
                "summary": (
                    f"Task {task_id} is still scheduled. Odoo declined to "
                    f"unschedule it — it is most likely not an FSM task."
                ),
                "changed_anything": False,
                "job": job,
            }
        return {
            "status": "unscheduled",
            "summary": f"Task {task_id} returned to the unscheduled backlog",
            "changed_anything": True,
            "job": job,
        }

    def jobs_on(self, date_from: str, date_to: str, limit: int = 100) -> list[dict]:
        """Jobs planned within a datetime window.

        Args:
            date_from: ``YYYY-MM-DD HH:MM:SS`` inclusive lower bound.
            date_to: ``YYYY-MM-DD HH:MM:SS`` inclusive upper bound.
        """
        return self.search(
            [["planned_date_begin", ">=", date_from],
             ["planned_date_begin", "<=", date_to]],
            limit=limit,
        )

    def jobs_for_customer(self, partner_id: int, limit: int = 50) -> list[dict]:
        """All FSM jobs for a customer."""
        return self.search([["partner_id", "=", partner_id]], limit=limit)

    def get_photos(self, task_id: int) -> list[dict]:
        """On-site photos attached to a job."""
        self._require()
        try:
            return self.client.search_read(
                self.IMAGE_MODEL, [["task_id", "=", task_id]],
                fields=_IMAGE_FIELDS, limit=100,
            )
        except Exception as exc:  # image model naming varies by version
            logger.info("Could not read %s for task %s: %s",
                        self.IMAGE_MODEL, task_id, exc)
            return []

    # ── Writes ───────────────────────────────────────────────────────

    def schedule(
        self,
        task_id: int,
        planned_date_begin: str,
        user_ids: Optional[list[int]] = None,
        deadline: Optional[str] = None,
    ) -> dict:
        """Put a job on the board at a given time, optionally assigning techs.

        Args:
            task_id: FSM task id.
            planned_date_begin: ``YYYY-MM-DD HH:MM:SS`` start.
            user_ids: Technicians to assign (replaces existing assignment).
            deadline: Optional ``date_deadline``.
        """
        values: dict[str, Any] = {"planned_date_begin": planned_date_begin}
        if user_ids:
            values["user_ids"] = [(6, 0, user_ids)]
        if deadline:
            values["date_deadline"] = deadline
        record = self.update(task_id, values)
        return {
            "summary": (
                f"Job '{record.get('name')}' scheduled for {planned_date_begin}"
                + (f" ({len(user_ids)} tech assigned)" if user_ids else "")
            ),
            "job": record,
        }

    def reschedule(self, task_id: int, planned_date_begin: str) -> dict:
        """Move a job to a new start time."""
        record = self.update(task_id, {"planned_date_begin": planned_date_begin})
        return {
            "summary": f"Job '{record.get('name')}' moved to {planned_date_begin}",
            "job": record,
        }

    # ── Summary ──────────────────────────────────────────────────────

    def dispatch_summary(self) -> dict:
        """Board state — what is scheduled, unscheduled, and done."""
        scheduled = self.count(
            [["planned_date_begin", "!=", False], ["fsm_done", "=", False]]
        )
        unscheduled = self.count(
            [["planned_date_begin", "=", False], ["fsm_done", "=", False]]
        )
        done = self.count([["fsm_done", "=", True]])
        return {
            "summary": (
                f"Field service: {scheduled} scheduled, "
                f"{unscheduled} awaiting dispatch, {done} completed"
            ),
            "scheduled": scheduled,
            "unscheduled": unscheduled,
            "completed": done,
        }
