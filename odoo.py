#!/usr/bin/env python3
"""
Odoo ERP CLI for OpenClaw.

Argparse-based CLI that dispatches subcommands to SmartActionHandler
and TodoMatrixOps methods, outputting JSON results to stdout.
"""
import argparse
import json
import os
import re
import sys

# Add skill directory to path
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

from odoo_skill import OdooClient, SmartActionHandler
from odoo_skill.errors import OdooError


# ── Permission model ────────────────────────────────────────────────
#
# TRUST MODEL: destructive CLI operations are authorized only for the
# authenticated Odoo API principal. This prevents caller-supplied identities
# from being treated as authorization inputs when the RPC still executes under
# the shared service account.

# Maps Odoo model prefixes to the group names required for destructive ops.
# For delete: user must have at least one of the listed Administrator groups.
_MODEL_PERMISSION_MAP = {
    "product.":      ["Inventory / Administrator"],
    "stock.":        ["Inventory / Administrator"],
    "sale.":         ["Sales / Administrator"],
    "purchase.":     ["Purchase / Administrator"],
    "account.":      ["Invoicing / Billing Administrator"],
    "crm.":          ["Sales / Administrator"],
    "project.":      ["Project / Administrator"],
    "hr.":           ["Employees / Administrator"],
    "mrp.":          ["Manufacturing / Administrator"],
    "res.partner":   ["Sales / Administrator"],
}

_MODEL_NAME_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


def _normalize_email(email: str) -> str:
    """Normalize an email/login string before comparing or querying."""
    return email.strip().lower()


def _validate_model_name(model: str) -> str:
    """Reject malformed model names before sending them to XML-RPC."""
    candidate = model.strip()
    if not _MODEL_NAME_RE.fullmatch(candidate):
        _err(f"Invalid Odoo model name: {model!r}", "ValidationError")
    return candidate


def _resolve_user(client: OdooClient, email: str) -> dict:
    """Look up an Odoo user by email and return their id, name, and group names."""
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return {}
    users = client.search_read(
        "res.users",
        [["login", "=", normalized_email]],
        fields=["id", "name", "groups_id"],
        limit=1,
    )
    if not users:
        return {}
    user = users[0]
    group_ids = user.get("groups_id", [])
    if group_ids:
        groups = client.search_read(
            "res.groups", [["id", "in", group_ids]], fields=["full_name"],
        )
        user["group_names"] = [g["full_name"] for g in groups]
    else:
        user["group_names"] = []
    return user


def _get_authenticated_user(client: OdooClient) -> dict:
    """Resolve the currently authenticated Odoo API principal."""
    user = _resolve_user(client, client.config.username)
    if not user:
        _err(
            "Authenticated Odoo API user could not be resolved in res.users.",
            "AuthenticationError",
        )
    user["login"] = _normalize_email(client.config.username)
    return user


def _check_permission(client: OdooClient, email: str, model: str, action: str) -> dict:
    """Check if a user has permission to perform an action on a model.

    Returns a dict with 'allowed' (bool) and 'reason' (str).
    This is a HARD check — the CLI refuses the operation if not allowed.
    """
    model = _validate_model_name(model)
    normalized_email = _normalize_email(email)
    normalized_action = action.strip().lower()

    if normalized_action != "delete":
        return {
            "allowed": False,
            "reason": "Local permission checks only support delete operations.",
        }

    user = _resolve_user(client, normalized_email)
    if not user:
        return {
            "allowed": False,
            "reason": f"No Odoo user found for {normalized_email}",
        }

    # Find required groups for this model
    required_groups = []
    for prefix, groups in _MODEL_PERMISSION_MAP.items():
        if model.startswith(prefix):
            required_groups = groups
            break

    if not required_groups:
        # No specific permission mapped — require Administration / Settings
        required_groups = ["Administration / Settings"]

    user_groups = set(user["group_names"])
    has_permission = any(g in user_groups for g in required_groups)

    if has_permission:
        return {
            "allowed": True,
            "reason": "User has required permission",
            "user": user["name"],
            "matched_group": next(g for g in required_groups if g in user_groups),
        }
    else:
        return {
            "allowed": False,
            "reason": "User lacks required permission for this operation",
            "user": user["name"],
        }


# ── Helpers ──────────────────────────────────────────────────────────


def _get_smart() -> SmartActionHandler:
    """Create an authenticated SmartActionHandler from env/config."""
    config_path = os.path.join(SKILL_DIR, "config.json")
    if not os.path.exists(config_path):
        _err("Odoo not configured. Copy config.json.template to config.json and fill in credentials.")
    client = OdooClient.from_env()
    return SmartActionHandler(client)


def _get_client() -> OdooClient:
    """Create an authenticated OdooClient."""
    return OdooClient.from_env()


def _parse_lines(raw: str) -> list[dict]:
    """Parse a JSON string into a list of product-line dicts.

    Accepts either a JSON array or a single JSON object (wrapped into
    a one-element list).

    Raises:
        SystemExit: on invalid JSON with a user-friendly message.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _err(f"Invalid JSON for --lines: {exc}")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    _err("--lines must be a JSON array or object")


def _out(data, **extra):
    """Print a success payload to stdout."""
    print(json.dumps(data, indent=2, default=str))


def _err(message: str, exc_type: str = "CLIError"):
    """Print an error payload to stderr and exit 1."""
    print(json.dumps({"error": message, "type": exc_type}, indent=2), file=sys.stderr)
    sys.exit(1)


# ── Subcommand handlers ─────────────────────────────────────────────


def cmd_create_quotation(args):
    smart = _get_smart()
    lines = _parse_lines(args.lines)
    result = smart.smart_create_quotation(
        customer_name=args.customer,
        product_lines=lines,
        notes=args.notes,
    )
    _out(result)


def cmd_create_lead(args):
    smart = _get_smart()
    result = smart.smart_create_lead(
        name=args.name,
        contact_name=args.contact,
        email=args.email,
        phone=args.phone,
        expected_revenue=args.expected_revenue,
    )
    _out(result)


def cmd_create_todo(args):
    smart = _get_smart()
    extra = {}
    if args.reminder:
        extra["reminder_datetime"] = args.reminder
    if args.category:
        extra["category_names"] = args.category
    result = smart.smart_create_todo(
        task_name=args.task,
        employee_name=args.employee,
        is_urgent=args.urgent,
        is_important=args.important,
        deadline=args.deadline,
        description=args.description,
        location_name=args.location,
        **extra,
    )
    _out(result)


def cmd_list_locations(args):
    smart = _get_smart()
    result = smart.todo_matrix.list_locations(
        search=args.search,
        limit=args.limit,
    )
    _out(result)


def cmd_learn_location(args):
    smart = _get_smart()
    result = smart.learn_location(phrase=args.phrase, target=args.target)
    _out(result)


def cmd_get_matrix(args):
    smart = _get_smart()
    result = smart.smart_get_matrix(employee_name=args.employee)
    _out(result)


def cmd_team_workload(args):
    smart = _get_smart()
    result = smart.smart_get_team_workload()
    _out(result)


def cmd_create_partner(args):
    smart = _get_smart()
    result = smart.find_or_create_partner(
        name=args.name,
        is_company=args.company,
        email=args.email or "",
        phone=args.phone or "",
    )
    _out(result)


def cmd_find_partner(args):
    smart = _get_smart()
    results = smart.partners.find_customer(query=args.name)
    _out(results)


def cmd_create_product(args):
    smart = _get_smart()
    defaults = {}
    if args.price is not None:
        defaults["list_price"] = args.price
    if args.type:
        defaults["type"] = args.type
    result = smart.find_or_create_product(name=args.name, **defaults)
    _out(result)


def cmd_find_product(args):
    smart = _get_smart()
    results = smart.client.search_read(
        "product.product",
        ["|", ["name", "ilike", args.name], ["default_code", "ilike", args.name]],
        fields=["id", "name", "default_code", "list_price", "type"],
        limit=20,
    )
    _out(results)


def cmd_list_todos(args):
    smart = _get_smart()
    employee_id = None
    if args.employee:
        employees = smart.todo_matrix.search_employees(query=args.employee)
        if not employees:
            _err(f"No employee found matching '{args.employee}'")
        employee_id = employees[0]["id"]
    result = smart.todo_matrix.search_tasks(
        employee_id=employee_id,
        quadrant=args.quadrant,
        state=args.state,
        is_overdue=args.overdue if args.overdue else None,
    )
    _out(result)


def cmd_start_task(args):
    smart = _get_smart()
    result = smart.todo_matrix.start_task(args.id)
    _out(result)


def cmd_complete_task(args):
    smart = _get_smart()
    result = smart.todo_matrix.complete_task(args.id)
    _out(result)


def cmd_cancel_task(args):
    smart = _get_smart()
    result = smart.todo_matrix.cancel_task(args.id)
    _out(result)


def cmd_get_task(args):
    smart = _get_smart()
    result = smart.todo_matrix.get_task(args.id)
    if not result:
        _err(f"Task {args.id} not found", "RecordNotFound")
    _out(result)


def cmd_update_task(args):
    smart = _get_smart()
    values = {}
    if args.name is not None:
        values["name"] = args.name
    if args.urgent is not None:
        values["is_urgent"] = args.urgent
    if args.important is not None:
        values["is_important"] = args.important
    if args.description is not None:
        values["description"] = args.description
    if args.deadline is not None:
        values["deadline"] = args.deadline
    if args.estimated_time is not None:
        values["estimated_time"] = args.estimated_time
    if args.state is not None:
        values["state"] = args.state
    if args.reminder is not None:
        values["reminder_datetime"] = args.reminder
    if args.no_location:
        values["location_id"] = False
    elif args.location is not None:
        values["location_id"] = smart._resolve_location_id(args.location)
    if args.no_categories:
        values["category_ids"] = []
    elif args.category:
        values["category_ids"] = smart.todo_matrix.resolve_category_ids(args.category)
    if not values:
        _err("No fields to update. Provide at least one optional field.")
    result = smart.todo_matrix.update_task(args.id, **values)
    _out(result)


def cmd_add_checklist(args):
    smart = _get_smart()
    result = smart.todo_matrix.add_checklist_item(
        task_id=args.task_id,
        name=args.name,
    )
    _out(result)


def cmd_toggle_checklist(args):
    smart = _get_smart()
    is_done = args.done  # True if --done, False if --undone
    result = smart.todo_matrix.toggle_checklist_item(
        item_id=args.id,
        is_done=is_done,
    )
    _out(result)


def cmd_get_checklist(args):
    smart = _get_smart()
    result = smart.todo_matrix.get_checklist(task_id=args.task_id)
    _out(result)


def cmd_attach_file(args):
    smart = _get_smart()
    result = smart.todo_matrix.add_attachment(
        task_id=args.task_id,
        file_path=args.file,
        filename=args.filename,
    )
    _out(result)


def cmd_list_attachments(args):
    smart = _get_smart()
    result = smart.todo_matrix.list_attachments(task_id=args.task_id)
    _out(result)


def cmd_delete_attachment(args):
    smart = _get_smart()
    smart.todo_matrix.delete_attachment(attachment_id=args.id)
    _out({"success": True, "deleted_attachment_id": args.id})


def cmd_search_employees(args):
    smart = _get_smart()
    result = smart.todo_matrix.search_employees(query=args.query)
    _out(result)


def cmd_get_categories(args):
    smart = _get_smart()
    result = smart.todo_matrix.get_categories()
    _out(result)


def cmd_check_stock(args):
    smart = _get_smart()
    results = smart.client.search_read(
        "product.product",
        ["|", ["name", "ilike", args.name], ["default_code", "ilike", args.name]],
        fields=["id", "name", "default_code", "qty_available", "virtual_available", "list_price", "type"],
        limit=20,
    )
    _out(results)


def cmd_check_permissions(args):
    client = _get_client()
    result = _check_permission(client, args.email, _validate_model_name(args.model), args.action)
    _out(result)


def cmd_delete_record(args):
    client = _get_client()
    model = _validate_model_name(args.model)
    authenticated_user = _get_authenticated_user(client)
    if args.as_user and _normalize_email(args.as_user) != authenticated_user["login"]:
        _err(
            "--as-user no longer supports impersonation. It must match the authenticated Odoo API user.",
            "PermissionDenied",
        )

    # Check and execute under the same authenticated Odoo principal.
    perm = _check_permission(client, authenticated_user["login"], model, "delete")
    if not perm["allowed"]:
        _err(
            f"PERMISSION DENIED: {authenticated_user['name']} cannot delete {model} records. {perm['reason']}",
            "PermissionDenied",
        )
    try:
        client.execute(model, "unlink", [args.record_id])
        _out({
            "success": True,
            "action": "delete",
            "model": model,
            "record_id": args.record_id,
            "authorized_by": authenticated_user["name"],
            "authorized_login": authenticated_user["login"],
            "matched_group": perm.get("matched_group", ""),
        })
    except Exception as exc:
        _err(f"Delete failed: {exc}", "DeleteError")


# ── Dispatch table ───────────────────────────────────────────────────

COMMANDS = {
    "create-quotation": cmd_create_quotation,
    "create-lead": cmd_create_lead,
    "create-todo": cmd_create_todo,
    "list-locations": cmd_list_locations,
    "learn-location": cmd_learn_location,
    "get-matrix": cmd_get_matrix,
    "team-workload": cmd_team_workload,
    "create-partner": cmd_create_partner,
    "find-partner": cmd_find_partner,
    "create-product": cmd_create_product,
    "find-product": cmd_find_product,
    "list-todos": cmd_list_todos,
    "start-task": cmd_start_task,
    "complete-task": cmd_complete_task,
    "cancel-task": cmd_cancel_task,
    "get-task": cmd_get_task,
    "update-task": cmd_update_task,
    "add-checklist": cmd_add_checklist,
    "toggle-checklist": cmd_toggle_checklist,
    "get-checklist": cmd_get_checklist,
    "attach-file": cmd_attach_file,
    "list-attachments": cmd_list_attachments,
    "delete-attachment": cmd_delete_attachment,
    "search-employees": cmd_search_employees,
    "get-categories": cmd_get_categories,
    "check-stock": cmd_check_stock,
    "check-permissions": cmd_check_permissions,
    "delete-record": cmd_delete_record,
}


# ── Argparse setup ───────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odoo.py",
        description="Odoo ERP CLI — dispatches subcommands to the Odoo connector.",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    # -- create-quotation
    p = subs.add_parser("create-quotation", help="Create a sales quotation")
    p.add_argument("--customer", required=True, help="Customer name")
    p.add_argument("--lines", required=True, help="Product lines as JSON array")
    p.add_argument("--notes", default=None, help="Order notes")

    # -- create-lead
    p = subs.add_parser("create-lead", help="Create a CRM lead")
    p.add_argument("--name", required=True, help="Lead title")
    p.add_argument("--contact", default=None, help="Contact person name")
    p.add_argument("--email", default=None, help="Contact email")
    p.add_argument("--phone", default=None, help="Contact phone")
    p.add_argument("--expected-revenue", type=float, default=None, help="Expected deal value")

    # -- create-todo
    p = subs.add_parser("create-todo", help="Create a to-do task in the priority matrix")
    p.add_argument("--task", required=True, help="Task title")
    p.add_argument("--employee", required=True, help="Employee name")
    p.add_argument("--urgent", action="store_true", default=False, help="Mark as urgent")
    p.add_argument("--important", action="store_true", default=False, help="Mark as important")
    p.add_argument("--deadline", default=None, help="Due date (YYYY-MM-DD)")
    p.add_argument("--reminder", default=None, help="Reminder datetime (YYYY-MM-DD HH:MM:SS, UTC)")
    p.add_argument("--description", default=None, help="Task description")
    p.add_argument(
        "--location", default=None,
        help=("Warehouse location phrase (e.g. '02-02-05', 'wh stock 02-02-05', "
              "'photo studio', 'MR09-B', 'rolling shelf B07'). Resolved against "
              "internal stock.location records. Fails loud if unresolved — "
              "run `list-locations` to browse."),
    )
    p.add_argument(
        "--category", action="append", default=None, metavar="NAME",
        help=("Tag the task with a category by name (e.g. 'Development'). "
              "Repeatable: pass --category multiple times for multi-tag. "
              "Case-insensitive; run `get-categories` to browse."),
    )

    # -- list-locations
    p = subs.add_parser(
        "list-locations",
        help="List internal warehouse stock.location records (read-only)",
    )
    p.add_argument("--search", default=None, help="Filter phrase (token-matched against complete_name)")
    p.add_argument("--limit", type=int, default=50, help="Max results (default 50)")

    # -- learn-location
    p = subs.add_parser(
        "learn-location",
        help=("Teach the resolver a new human-phrase → location alias. "
              "Requires prior confirmation from Ian. Target must resolve to an "
              "existing internal stock.location."),
    )
    p.add_argument("--phrase", required=True, help="Human phrase to learn (e.g. 'receiving dock')")
    p.add_argument("--target", required=True,
                   help="Canonical location phrase the resolver should map to (e.g. 'MR09-B')")

    # -- get-matrix
    p = subs.add_parser("get-matrix", help="Get an employee's Eisenhower priority matrix")
    p.add_argument("--employee", required=True, help="Employee name")

    # -- team-workload
    subs.add_parser("team-workload", help="Get team workload dashboard")

    # -- create-partner
    p = subs.add_parser("create-partner", help="Find or create a partner/contact")
    p.add_argument("--name", required=True, help="Partner name")
    p.add_argument("--email", default=None, help="Email address")
    p.add_argument("--phone", default=None, help="Phone number")
    p.add_argument("--company", action="store_true", default=False, help="Create as company")

    # -- find-partner
    p = subs.add_parser("find-partner", help="Search for a partner by name")
    p.add_argument("--name", required=True, help="Search query")

    # -- create-product
    p = subs.add_parser("create-product", help="Find or create a product")
    p.add_argument("--name", required=True, help="Product name")
    p.add_argument("--price", type=float, default=None, help="List price")
    p.add_argument("--type", default=None, help="Product type (consu, service, product)")

    # -- find-product
    p = subs.add_parser("find-product", help="Search for a product by name")
    p.add_argument("--name", required=True, help="Search query")

    # -- list-todos
    p = subs.add_parser("list-todos", help="List to-do tasks with filters")
    p.add_argument("--employee", default=None, help="Employee name filter")
    p.add_argument("--quadrant", default=None, choices=["do", "schedule", "delegate", "eliminate"],
                   help="Eisenhower quadrant filter")
    p.add_argument("--state", default=None, choices=["todo", "in_progress", "done", "cancelled"],
                   help="Task state filter")
    p.add_argument("--overdue", action="store_true", default=False, help="Show only overdue tasks")

    # -- start-task
    p = subs.add_parser("start-task", help="Move a task to In Progress")
    p.add_argument("--id", type=int, required=True, help="Task ID")

    # -- complete-task
    p = subs.add_parser("complete-task", help="Mark a task as done")
    p.add_argument("--id", type=int, required=True, help="Task ID")

    # -- cancel-task
    p = subs.add_parser("cancel-task", help="Cancel a task")
    p.add_argument("--id", type=int, required=True, help="Task ID")

    # -- get-task
    p = subs.add_parser("get-task", help="Get full details of a task")
    p.add_argument("--id", type=int, required=True, help="Task ID")

    # -- update-task
    p = subs.add_parser("update-task", help="Update a task's fields")
    p.add_argument("--id", type=int, required=True, help="Task ID")
    p.add_argument("--name", default=None, help="New task name")
    p.add_argument("--urgent", action="store_true", default=None, help="Set urgent flag")
    p.add_argument("--no-urgent", dest="urgent", action="store_false", help="Clear urgent flag")
    p.add_argument("--important", action="store_true", default=None, help="Set important flag")
    p.add_argument("--no-important", dest="important", action="store_false", help="Clear important flag")
    p.add_argument("--description", default=None, help="Task description")
    p.add_argument("--deadline", default=None, help="Due date (YYYY-MM-DD)")
    p.add_argument("--estimated-time", type=float, default=None, help="Estimated hours")
    p.add_argument("--state", default=None, choices=["todo", "in_progress", "done", "cancelled"],
                   help="Task state")
    p.add_argument("--reminder", default=None, help="Reminder datetime (YYYY-MM-DD HH:MM:SS, UTC)")
    p.add_argument(
        "--location", default=None,
        help=("Warehouse location phrase (e.g. '02-02-05', 'front lobby'). "
              "Resolved against internal stock.location records. Fails loud "
              "if unresolved — run `list-locations` to browse."),
    )
    p.add_argument(
        "--no-location", action="store_true", default=False,
        help="Clear the task's location (sets location_id to False in Odoo).",
    )
    p.add_argument(
        "--category", action="append", default=None, metavar="NAME",
        help=("Replace task categories with these names. Repeatable. "
              "Case-insensitive; fails loud if unknown. "
              "Run `get-categories` to browse."),
    )
    p.add_argument(
        "--no-categories", action="store_true", default=False,
        help="Clear all categories on the task.",
    )

    # -- add-checklist
    p = subs.add_parser("add-checklist", help="Add a checklist item to a task")
    p.add_argument("--task-id", type=int, required=True, help="Task ID")
    p.add_argument("--name", required=True, help="Checklist item text")

    # -- toggle-checklist
    p = subs.add_parser("toggle-checklist", help="Toggle a checklist item done/undone")
    p.add_argument("--id", type=int, required=True, help="Checklist item ID")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--done", action="store_true", default=False, help="Mark as done")
    group.add_argument("--undone", dest="done", action="store_false", help="Mark as not done")

    # -- get-checklist
    p = subs.add_parser("get-checklist", help="Get checklist items for a task")
    p.add_argument("--task-id", type=int, required=True, help="Task ID")

    # -- attach-file
    p = subs.add_parser("attach-file", help="Attach a file (photo, document) to a task")
    p.add_argument("--task-id", type=int, required=True, help="Task ID to attach to")
    p.add_argument("--file", required=True, help="Absolute path to the file on disk")
    p.add_argument("--filename", default=None, help="Display name in Odoo (defaults to file basename)")

    # -- list-attachments
    p = subs.add_parser("list-attachments", help="List attachments on a task")
    p.add_argument("--task-id", type=int, required=True, help="Task ID")

    # -- delete-attachment
    p = subs.add_parser("delete-attachment", help="Delete an attachment by ID")
    p.add_argument("--id", type=int, required=True, help="Attachment ID")

    # -- search-employees
    p = subs.add_parser("search-employees", help="Search active employees")
    p.add_argument("--query", default=None, help="Name search query")

    # -- get-categories
    subs.add_parser("get-categories", help="List all task categories")

    # -- check-stock
    p = subs.add_parser("check-stock", help="Check stock levels for a product")
    p.add_argument("--name", required=True, help="Product name or SKU to search")

    # -- check-permissions
    p = subs.add_parser("check-permissions", help="Check if a user has permission for an action")
    p.add_argument("--email", required=True, help="User's email address")
    p.add_argument("--model", required=True, help="Odoo model (e.g. product.product, sale.order)")
    p.add_argument("--action", default="delete", choices=["delete"],
                   help="Action to check (delete only)")

    # -- delete-record
    p = subs.add_parser("delete-record", help="Delete a record as the authenticated Odoo API user")
    p.add_argument("--model", required=True, help="Odoo model (e.g. product.product)")
    p.add_argument("--record-id", type=int, required=True, help="Record ID to delete")
    p.add_argument("--as-user", help="Deprecated: must match the authenticated Odoo API user if provided")

    return parser


# ── Main ─────────────────────────────────────────────────────────────


def main():
    parser = build_parser()
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if handler is None:
        _err(f"Unknown command: {args.command}")

    try:
        handler(args)
    except OdooError as exc:
        _err(str(exc), exc.__class__.__name__)
    except ValueError as exc:
        _err(str(exc), "ValueError")
    except Exception as exc:
        _err(str(exc), "UnexpectedError")


if __name__ == "__main__":
    main()
