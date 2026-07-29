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
    "description", "partner_phone", "partner_email",
    "fsm_signed_by", "fsm_confirmation_sent", "fsm_reminder_sent",
    "sms_fsm_confirmation_sent", "sms_fsm_reminder_sent",
    "sms_fsm_completed_sent", "fsm_image_ids", "syncro_ticket_id",
    "tag_ids", "kanban_state",
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
    #: ``project.task.is_fsm`` is a *related* non-stored field, so filtering
    #: on it directly is silently ignored by Odoo and returns every project
    #: task. The scope therefore goes through the stored source of truth,
    #: ``project.project.is_fsm``, via the task's stored ``project_id``.
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
