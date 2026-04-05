"""
Employee To-Do Priority Matrix operations for Odoo.

Manages ``employee.todo.task`` records with Eisenhower Matrix quadrants
(urgent/important), checklist items, and team workload data.

Requires the ``employee_todo_matrix`` Odoo module to be installed.
"""

import logging
from typing import Any, Optional

from ..client import OdooClient

logger = logging.getLogger("odoo_skill")

_TASK_LIST_FIELDS = [
    "id", "name", "employee_id", "is_urgent", "is_important",
    "eisenhower_quadrant", "state", "priority", "deadline",
    "estimated_time", "is_overdue",
]

_TASK_DETAIL_FIELDS = _TASK_LIST_FIELDS + [
    "description", "date_start", "date_end", "date_done",
    "checklist_progress", "category_ids", "create_date",
]

_CHECKLIST_FIELDS = [
    "id", "name", "sequence", "is_done", "done_date",
]

_CATEGORY_FIELDS = [
    "id", "name", "color",
]


class TodoMatrixOps:
    """High-level operations for the Employee To-Do Priority Matrix.

    Provides CRUD for to-do tasks with Eisenhower quadrant support,
    checklist management, and team workload queries.

    Args:
        client: An authenticated :class:`OdooClient` instance.
    """

    TASK_MODEL = "employee.todo.task"
    CHECKLIST_MODEL = "employee.todo.checklist"
    CATEGORY_MODEL = "employee.todo.category"
    WORKLOAD_MODEL = "employee.todo.workload"

    def __init__(self, client: OdooClient) -> None:
        self.client = client

    # ── Task CRUD ─────────────────────────────────────────────────────

    def create_task(
        self,
        name: str,
        employee_id: int,
        is_urgent: bool = False,
        is_important: bool = False,
        description: Optional[str] = None,
        deadline: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        estimated_time: Optional[float] = None,
        priority: Optional[str] = None,
        category_ids: Optional[list[int]] = None,
        **extra: Any,
    ) -> dict:
        """Create a new to-do task in the priority matrix.

        Args:
            name: Task title.
            employee_id: The employee this task is assigned to.
            is_urgent: Whether the task is urgent (Eisenhower axis).
            is_important: Whether the task is important (Eisenhower axis).
            description: Task description (HTML supported).
            deadline: Due date as ``YYYY-MM-DD``.
            date_start: Start datetime as ``YYYY-MM-DDTHH:MM:SS``.
            date_end: End datetime as ``YYYY-MM-DDTHH:MM:SS``.
            estimated_time: Estimated hours to complete.
            priority: Priority level (``'0'``=normal, ``'1'``=low,
                ``'2'``=high, ``'3'``=urgent).
            category_ids: List of category IDs to tag the task with.
            **extra: Additional ``employee.todo.task`` field values.

        Returns:
            The newly created task record.
        """
        values: dict[str, Any] = {
            "name": name,
            "employee_id": employee_id,
            "is_urgent": is_urgent,
            "is_important": is_important,
        }
        if description:
            values["description"] = description
        if deadline:
            values["deadline"] = deadline
        if date_start:
            values["date_start"] = date_start
        if date_end:
            values["date_end"] = date_end
        if estimated_time is not None:
            values["estimated_time"] = estimated_time
        if priority:
            values["priority"] = priority
        if category_ids:
            values["category_ids"] = [(6, 0, category_ids)]
        values.update(extra)

        task_id = self.client.create(self.TASK_MODEL, values)
        logger.info("Created todo task %r for employee=%d → id=%d", name, employee_id, task_id)
        return self._read_task(task_id)

    def get_task(self, task_id: int) -> dict:
        """Get full details of a to-do task.

        Args:
            task_id: The task ID.

        Returns:
            Task record dict, or empty dict if not found.
        """
        return self._read_task(task_id)

    def update_task(self, task_id: int, **values: Any) -> dict:
        """Update a to-do task's fields.

        Args:
            task_id: The task ID.
            **values: Field values to update. Supports ``name``,
                ``is_urgent``, ``is_important``, ``description``,
                ``deadline``, ``estimated_time``, ``priority``, etc.

        Returns:
            The updated task record.
        """
        if "category_ids" in values and isinstance(values["category_ids"], list):
            values["category_ids"] = [(6, 0, values["category_ids"])]

        self.client.write(self.TASK_MODEL, task_id, values)
        logger.info("Updated todo task id=%d: %s", task_id, list(values.keys()))
        return self._read_task(task_id)

    def search_tasks(
        self,
        employee_id: Optional[int] = None,
        quadrant: Optional[str] = None,
        state: Optional[str] = None,
        is_overdue: Optional[bool] = None,
        query: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Search to-do tasks with optional filters.

        Args:
            employee_id: Filter by assigned employee.
            quadrant: Filter by Eisenhower quadrant
                (``do``, ``schedule``, ``delegate``, ``eliminate``).
            state: Filter by state
                (``todo``, ``in_progress``, ``done``, ``cancelled``).
            is_overdue: Filter to only overdue tasks.
            query: Text to search in task name.
            limit: Max results (default 50, max 200).
            offset: Pagination offset.

        Returns:
            List of matching task records.
        """
        domain: list = []
        if employee_id:
            domain.append(["employee_id", "=", employee_id])
        if quadrant:
            domain.append(["eisenhower_quadrant", "=", quadrant])
        if state:
            domain.append(["state", "=", state])
        if is_overdue is True:
            domain.append(["is_overdue", "=", True])
        if query:
            domain.append(["name", "ilike", query])

        limit = min(limit, 200)

        return self.client.search_read(
            self.TASK_MODEL, domain,
            fields=_TASK_LIST_FIELDS, limit=limit, offset=offset,
            order="id desc",
        )

    # ── State transitions ─────────────────────────────────────────────

    def start_task(self, task_id: int) -> dict:
        """Move a task to 'In Progress' state.

        Args:
            task_id: The task ID.

        Returns:
            The updated task record.
        """
        self.client.execute(self.TASK_MODEL, "action_start", [task_id])
        logger.info("Started todo task id=%d", task_id)
        return self._read_task(task_id)

    def complete_task(self, task_id: int) -> dict:
        """Mark a task as done.

        Args:
            task_id: The task ID.

        Returns:
            The updated task record.
        """
        self.client.execute(self.TASK_MODEL, "action_done", [task_id])
        logger.info("Completed todo task id=%d", task_id)
        return self._read_task(task_id)

    def cancel_task(self, task_id: int) -> dict:
        """Cancel a task.

        Args:
            task_id: The task ID.

        Returns:
            The updated task record.
        """
        self.client.execute(self.TASK_MODEL, "action_cancel", [task_id])
        logger.info("Cancelled todo task id=%d", task_id)
        return self._read_task(task_id)

    def reset_task(self, task_id: int) -> dict:
        """Reset a task back to 'To Do' state.

        Args:
            task_id: The task ID.

        Returns:
            The updated task record.
        """
        self.client.execute(self.TASK_MODEL, "action_reset", [task_id])
        logger.info("Reset todo task id=%d", task_id)
        return self._read_task(task_id)

    # ── Eisenhower Matrix ─────────────────────────────────────────────

    def get_matrix(self, employee_id: int) -> dict:
        """Get an employee's tasks organized by Eisenhower quadrant.

        Returns tasks grouped into the four quadrants for active
        (non-done, non-cancelled) tasks only.

        Args:
            employee_id: The employee ID.

        Returns:
            Dict with ``do``, ``schedule``, ``delegate``, ``eliminate``
            lists, plus ``summary`` counts.
        """
        domain = [
            ["employee_id", "=", employee_id],
            ["state", "in", ["todo", "in_progress"]],
        ]
        tasks = self.client.search_read(
            self.TASK_MODEL, domain,
            fields=_TASK_LIST_FIELDS, limit=200,
            order="priority desc, deadline asc",
        )

        matrix: dict[str, list] = {
            "do": [],
            "schedule": [],
            "delegate": [],
            "eliminate": [],
        }

        for task in tasks:
            q = task.get("eisenhower_quadrant", "eliminate")
            if q in matrix:
                matrix[q].append(task)

        return {
            **matrix,
            "summary": {
                "do": len(matrix["do"]),
                "schedule": len(matrix["schedule"]),
                "delegate": len(matrix["delegate"]),
                "eliminate": len(matrix["eliminate"]),
                "total": len(tasks),
            },
        }

    # ── Team Workload ─────────────────────────────────────────────────

    def get_team_workload(self) -> dict:
        """Get team workload data from the workload SQL view.

        Returns per-employee task counts, quadrant breakdowns,
        and team-wide totals.

        Returns:
            Dict with ``team_totals`` and ``employees`` list.
        """
        result = self.client.execute(
            self.WORKLOAD_MODEL, "get_workload_data", [],
        )
        return result

    # ── Checklist ─────────────────────────────────────────────────────

    def get_checklist(self, task_id: int) -> list[dict]:
        """Get checklist items for a task.

        Args:
            task_id: The task ID.

        Returns:
            List of checklist item records.
        """
        return self.client.search_read(
            self.CHECKLIST_MODEL,
            [["task_id", "=", task_id]],
            fields=_CHECKLIST_FIELDS,
            order="sequence asc, id asc",
        )

    def add_checklist_item(
        self,
        task_id: int,
        name: str,
        sequence: int = 10,
    ) -> dict:
        """Add a checklist item to a task.

        Args:
            task_id: The task ID.
            name: Checklist item text.
            sequence: Sort order (default 10).

        Returns:
            The newly created checklist item record.
        """
        item_id = self.client.create(self.CHECKLIST_MODEL, {
            "task_id": task_id,
            "name": name,
            "sequence": sequence,
        })
        logger.info("Added checklist item %r to task %d → id=%d", name, task_id, item_id)
        records = self.client.read(
            self.CHECKLIST_MODEL, item_id, fields=_CHECKLIST_FIELDS,
        )
        return records[0] if records else {}

    def toggle_checklist_item(self, item_id: int, is_done: bool) -> dict:
        """Mark a checklist item as done or not done.

        Args:
            item_id: The checklist item ID.
            is_done: Whether the item is completed.

        Returns:
            The updated checklist item record.
        """
        self.client.write(self.CHECKLIST_MODEL, item_id, {"is_done": is_done})
        logger.info("Toggled checklist item id=%d → is_done=%s", item_id, is_done)
        records = self.client.read(
            self.CHECKLIST_MODEL, item_id, fields=_CHECKLIST_FIELDS,
        )
        return records[0] if records else {}

    # ── Categories ────────────────────────────────────────────────────

    def get_categories(self) -> list[dict]:
        """Get all available task categories.

        Returns:
            List of category records.
        """
        return self.client.search_read(
            self.CATEGORY_MODEL, [],
            fields=_CATEGORY_FIELDS,
            order="name asc",
        )

    # ── Employees (convenience) ───────────────────────────────────────

    def search_employees(
        self,
        query: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search active employees for task assignment.

        Args:
            query: Text to search in employee name.
            limit: Max results.

        Returns:
            List of employee records with id, name, job_title,
            department, and email.
        """
        domain: list = [["active", "=", True]]
        if query:
            domain.append(["name", "ilike", query])

        return self.client.search_read(
            "hr.employee", domain,
            fields=["id", "name", "job_title", "department_id", "work_email"],
            limit=min(limit, 200),
            order="name asc",
        )

    # ── Internal helpers ──────────────────────────────────────────────

    def _read_task(self, task_id: int) -> dict:
        """Read a single task by ID with full details."""
        records = self.client.read(
            self.TASK_MODEL, task_id, fields=_TASK_DETAIL_FIELDS,
        )
        return records[0] if records else {}
