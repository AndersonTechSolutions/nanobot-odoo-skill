"""
Employee To-Do Priority Matrix operations for Odoo.

Manages ``employee.todo.task`` records with Eisenhower Matrix quadrants
(urgent/important), checklist items, and team workload data.

Requires the ``employee_todo_matrix`` Odoo module to be installed.
"""

import base64
import logging
import mimetypes
import os
import re
from html import escape as _html_escape
from typing import Any, Optional

from ..client import OdooClient

logger = logging.getLogger("odoo_skill")

_HTML_TAG_RE = re.compile(r"<[a-zA-Z/!][^>]*>")
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")


def _format_description_html(text: Optional[str]) -> Optional[str]:
    """Normalize free-text descriptions into Odoo-friendly HTML.

    Odoo's ``employee.todo.task.description`` is an HTML field. If Andy
    (or any caller) passes raw plain text, Odoo wraps the whole thing in
    a single ``<p>`` and newlines collapse — so bullet lists paste as one
    run-on paragraph.

    This helper:

    - passes HTML through unchanged (if any tag is detected)
    - converts bullet-prefixed lines (``-``, ``*``, ``•``, ``1.``) into
      ``<ul><li>…</li></ul>``
    - treats blank-line-separated blocks as paragraphs
    - collapses single newlines inside a paragraph into spaces
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None

    # If the caller already sent HTML, trust them.
    if _HTML_TAG_RE.search(text):
        return text

    # Split into blocks separated by blank lines.
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    out: list[str] = []

    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        bullet_items = [_BULLET_LINE_RE.match(ln) for ln in lines]
        if lines and all(bullet_items):
            items = "".join(
                f"<li>{_html_escape(m.group(1).strip())}</li>" for m in bullet_items  # type: ignore[union-attr]
            )
            out.append(f"<ul>{items}</ul>")
        else:
            joined = " ".join(ln.strip() for ln in lines)
            out.append(f"<p>{_html_escape(joined)}</p>")

    return "".join(out)

_TASK_LIST_FIELDS = [
    "id", "name", "employee_ids", "primary_employee_id",
    "is_urgent", "is_important",
    "eisenhower_quadrant", "state", "priority", "deadline",
    "estimated_time", "is_overdue", "date_start", "create_date",
    "reminder_datetime", "reminder_sent",
]

_TASK_DETAIL_FIELDS = _TASK_LIST_FIELDS + [
    "description", "date_start", "date_end", "date_done",
    "checklist_progress", "category_ids", "create_date",
    "location_id",
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
    CATEGORY_MODEL = "todo.task.category"
    WORKLOAD_MODEL = "employee.todo.workload"
    LOCATION_MODEL = "stock.location"

    def __init__(self, client: OdooClient) -> None:
        self.client = client

    # ── Task CRUD ─────────────────────────────────────────────────────

    def create_task(
        self,
        name: str,
        employee_id: Optional[int] = None,
        employee_ids: Optional[list[int]] = None,
        primary_employee_id: Optional[int] = None,
        is_urgent: bool = False,
        is_important: bool = False,
        description: Optional[str] = None,
        deadline: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        estimated_time: Optional[float] = None,
        priority: Optional[str] = None,
        category_ids: Optional[list[int]] = None,
        reminder_datetime: Optional[str] = None,
        location_id: Optional[int] = None,
        **extra: Any,
    ) -> dict:
        """Create a new to-do task in the priority matrix.

        ``employee.todo.task`` supports multi-assignee. Pass a list via
        ``employee_ids`` for shared tasks; the legacy single-employee
        ``employee_id`` parameter is still accepted for backward compat
        and is translated to a one-item ``employee_ids`` plus
        ``primary_employee_id``.

        Args:
            name: Task title.
            employee_id: Single assignee (legacy). When set without
                ``employee_ids`` the task gets one assignee, with this
                employee as primary.
            employee_ids: Full assignee list. Required if ``employee_id``
                is not provided. The task must have at least one assignee.
            primary_employee_id: The "owner" assignee — drives Todoist
                sync, reminders, and kanban grouping. Defaults to the
                first id in ``employee_ids``. Must appear in
                ``employee_ids``.
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
            reminder_datetime: When to remind the primary assignee, as
                ``YYYY-MM-DD HH:MM:SS`` (UTC).
            **extra: Additional ``employee.todo.task`` field values.

        Returns:
            The newly created task record.
        """
        if not employee_ids and employee_id:
            employee_ids = [employee_id]
        if not employee_ids:
            raise ValueError(
                "create_task requires employee_ids (or legacy employee_id)."
            )
        if primary_employee_id is None:
            primary_employee_id = employee_ids[0]
        elif primary_employee_id not in employee_ids:
            raise ValueError(
                f"primary_employee_id={primary_employee_id} must be one of "
                f"employee_ids={employee_ids}."
            )

        values: dict[str, Any] = {
            "name": name,
            "employee_ids": [(6, 0, employee_ids)],
            "primary_employee_id": primary_employee_id,
            "is_urgent": is_urgent,
            "is_important": is_important,
        }
        formatted_description = _format_description_html(description)
        if formatted_description:
            values["description"] = formatted_description
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
        if reminder_datetime:
            values["reminder_datetime"] = reminder_datetime
        if location_id:
            values["location_id"] = location_id
        values.update(extra)

        task_id = self.client.create(self.TASK_MODEL, values)
        logger.info(
            "Created todo task %r for employees=%s primary=%d → id=%d",
            name, employee_ids, primary_employee_id, task_id,
        )
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
        if "description" in values:
            formatted = _format_description_html(values["description"])
            if formatted is None:
                values.pop("description")
            else:
                values["description"] = formatted

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
            employee_id: Filter to tasks where this employee is one of
                the assignees (covers both single- and multi-assignee
                tasks; matches against ``employee_ids``).
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
            domain.append(["employee_ids", "in", [employee_id]])
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
            ["employee_ids", "in", [employee_id]],
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

    def resolve_category_ids(self, names: list[str]) -> list[int]:
        """Resolve category names to ids. Fails loud on unmatched/ambiguous.

        Case-insensitive exact match preferred; falls back to ilike. Raises
        ValueError with the available category list if any name is unknown,
        so the caller can surface a useful error to the user.
        """
        if not names:
            return []
        all_cats = self.get_categories()
        by_lower = {c["name"].lower(): c for c in all_cats}
        resolved: list[int] = []
        for raw in names:
            phrase = raw.strip()
            if not phrase:
                continue
            exact = by_lower.get(phrase.lower())
            if exact:
                resolved.append(exact["id"])
                continue
            matches = [c for c in all_cats if phrase.lower() in c["name"].lower()]
            if len(matches) == 1:
                resolved.append(matches[0]["id"])
            elif len(matches) > 1:
                raise ValueError(
                    f"Category '{raw}' is ambiguous. Matched: "
                    f"{[m['name'] for m in matches]}. Use the exact name."
                )
            else:
                raise ValueError(
                    f"Unknown category '{raw}'. Available: "
                    f"{[c['name'] for c in all_cats]}"
                )
        return resolved

    # ── Locations (convenience) ───────────────────────────────────────

    def list_locations(
        self,
        search: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """List internal warehouse ``stock.location`` records.

        Use this when a resolver phrase is ambiguous or unfamiliar —
        e.g. the user asks "what stock locations do we have?" or
        picked a name the resolver can't pin down. Read-only: this
        skill never creates ``stock.location`` records; that is a
        manager-only action inside Odoo.

        Token-based filter: ``search="wh stock"`` matches anything
        whose ``complete_name`` contains both ``wh`` and ``stock``.

        Args:
            search: Optional phrase matched against ``complete_name``.
                Tokens split on whitespace, slashes, hyphens, commas,
                and underscores; every token must appear somewhere
                in the record's ``complete_name``.
            limit: Max results. Default 50.

        Returns:
            List of dicts with ``id``, ``name``, and ``complete_name``,
            ordered by ``complete_name``.
        """
        domain: list = [["usage", "=", "internal"]]
        if search and search.strip():
            tokens = [t for t in re.split(r"[\s/,\-_]+", search.strip()) if t]
            for tok in tokens:
                domain.append(["complete_name", "ilike", tok])

        return self.client.search_read(
            self.LOCATION_MODEL,
            domain,
            fields=["id", "name", "complete_name"],
            limit=limit,
            order="complete_name",
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

    # ── Attachments ──────────────────────────────────────────────────

    def add_attachment(
        self,
        task_id: int,
        file_path: str,
        filename: Optional[str] = None,
    ) -> dict:
        """Attach a file to a to-do task and post it in the task chatter.

        Creates an ``ir.attachment`` and then posts it via ``message_post``
        on the task record. ``employee.todo.task`` inherits ``mail.thread``,
        and Odoo's form view only renders attachments that came through the
        chatter — a raw ``ir.attachment`` linked by ``res_id`` alone exists
        in the DB but is invisible in the UI.

        Args:
            task_id: The task ID to attach to.
            file_path: Absolute path to the file on disk.
            filename: Display name in Odoo. Defaults to the file's basename.

        Returns:
            The created attachment record plus ``message_id`` of the chatter
            post that surfaces it in the UI.
        """
        if not os.path.isfile(file_path):
            raise ValueError(f"File not found: {file_path}")

        if filename is None:
            filename = os.path.basename(file_path)

        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        with open(file_path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")

        attachment_id = self.client.create("ir.attachment", {
            "name": filename,
            "datas": data,
            "mimetype": mimetype,
            "res_model": self.TASK_MODEL,
            "res_id": task_id,
        })
        logger.info("Created ir.attachment id=%d for %r on task %d", attachment_id, filename, task_id)

        # Link the attachment into the task's Attachments tab (attachment_ids m2m).
        # Command tuple (4, id) = link existing record. This is what populates the
        # dedicated "Attachments" tab on the task form view.
        self.client.write(self.TASK_MODEL, [task_id], {
            "attachment_ids": [(4, attachment_id)],
        })
        logger.info("Linked attachment %d into task %d attachment_ids", attachment_id, task_id)

        # Also post through the chatter so there's a dated audit entry.
        message_id = self.client.execute(
            self.TASK_MODEL, "message_post", [task_id],
            body=f"Attached: {filename}",
            attachment_ids=[attachment_id],
        )
        logger.info("Posted attachment to task %d chatter → message id=%s", task_id, message_id)

        records = self.client.read(
            "ir.attachment", attachment_id,
            fields=["id", "name", "mimetype", "file_size", "create_date"],
        )
        result = records[0] if records else {}
        result["message_id"] = message_id
        return result

    def list_attachments(self, task_id: int) -> list[dict]:
        """List all attachments on a to-do task.

        Args:
            task_id: The task ID.

        Returns:
            List of attachment records.
        """
        return self.client.search_read(
            "ir.attachment",
            [["res_model", "=", self.TASK_MODEL], ["res_id", "=", task_id]],
            fields=["id", "name", "mimetype", "file_size", "create_date"],
            order="create_date desc",
        )

    def delete_attachment(self, attachment_id: int) -> bool:
        """Delete an attachment by ID.

        Args:
            attachment_id: The attachment record ID.

        Returns:
            True on success.
        """
        self.client.unlink("ir.attachment", attachment_id)
        logger.info("Deleted attachment id=%d", attachment_id)
        return True

    # ── Internal helpers ──────────────────────────────────────────────

    def _read_task(self, task_id: int) -> dict:
        """Read a single task by ID with full details."""
        records = self.client.read(
            self.TASK_MODEL, task_id, fields=_TASK_DETAIL_FIELDS,
        )
        return records[0] if records else {}
