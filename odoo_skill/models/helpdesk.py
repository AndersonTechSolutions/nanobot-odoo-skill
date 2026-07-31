"""
Helpdesk operations for the ``atech_helpdesk`` module (Odoo 17 Enterprise
``helpdesk.ticket`` plus AndersonTech extensions).

The extensions add: linked repairs/RMAs/to-dos, an eBay order bridge with
exception events (``atech.helpdesk.ebay.event``), eBay buyer messaging, and
AI-drafted replies (``ai_draft`` / ``ai_draft_state``).

Note the AI draft flow is deliberately two-step: ``action_ai_draft_reply``
generates into ``ai_draft``, and a separate ``action_post_ai_draft`` (or
``action_send_ai_draft_via_ebay``) publishes it. Both are allowlisted, but
:meth:`HelpdeskOps.draft_ai_reply` only generates — publishing stays an
explicit second call so an agent cannot accidentally message a customer.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "partner_id", "stage_id", "priority", "user_id",
    "team_id", "ticket_type_id", "kanban_state", "create_date",
]
_DETAIL_FIELDS = _LIST_FIELDS + [
    "description", "partner_email", "partner_phone", "tag_ids",
    "close_date", "sla_deadline",
    # AndersonTech links
    "repair_ids", "repairs_count", "rma_ids", "rma_count",
    "fsm_task_ids", "fsm_task_count", "todo_task_ids", "todo_task_count",
    # eBay bridge
    "ebay_order_id", "ebay_order_number", "ebay_order_status",
    "ebay_order_total", "ebay_order_item_name", "ebay_order_link",
    "ebay_message_count",
    "atech_ebay_event_kind", "atech_ebay_event_status",
    "atech_ebay_action_needed", "atech_ebay_respond_by_at",
    "atech_ebay_amount_value", "atech_ebay_rma_order_id",
    # AI drafting
    "ai_draft", "ai_draft_state", "ai_draft_date",
    "ai_draft_error", "ai_draft_warning",
]

_EVENT_FIELDS = [
    "id", "display_name", "create_date",
]


class HelpdeskOps(BaseOps):
    """Operations on helpdesk tickets and their AndersonTech extensions."""

    MODEL = "helpdesk.ticket"
    MODULE = "atech_helpdesk"
    EVENT_MODEL = "atech.helpdesk.ebay.event"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "priority desc, create_date desc"

    ALLOWED_ACTIONS = frozenset({
        # AI drafting
        "action_ai_draft_reply",
        "action_post_ai_draft",
        "action_dismiss_ai_draft",
        "action_send_ai_draft_via_ebay",
        # downstream record creation
        "action_create_rma",
        "action_generate_fsm_task",
        # eBay bridge
        "action_atech_rescan_ebay_order",
        "action_atech_import_ebay_photos",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def open_tickets(self, limit: int = 50) -> list[dict]:
        """Tickets that are not closed."""
        return self.search([["close_date", "=", False]], limit=limit)

    def tickets_for_customer(self, partner_id: int, limit: int = 50) -> list[dict]:
        """All tickets for a customer."""
        return self.search([["partner_id", "=", partner_id]], limit=limit)

    def assigned_to(self, user_id: int, limit: int = 50) -> list[dict]:
        """Open tickets assigned to an agent."""
        return self.search(
            [["user_id", "=", user_id], ["close_date", "=", False]], limit=limit
        )

    def ebay_action_needed(self, limit: int = 50) -> list[dict]:
        """eBay-sourced tickets flagged as needing a response.

        These carry a ``respond_by`` deadline from eBay — missing it has
        seller-metric consequences, so this is the highest-priority queue.
        """
        return self.search(
            [["atech_ebay_action_needed", "=", True]],
            limit=limit, order="atech_ebay_respond_by_at asc",
        )

    def find_by_ebay_order(self, order_number: str, limit: int = 10) -> list[dict]:
        """Locate tickets attached to an eBay order number.

        ``ebay_order_number`` is non-stored but searchable — it is
        ``related="ebay_order_id.order_id"``, and Odoo rewrites the domain to
        that stored target. Falls back to the stored
        ``atech_ebay_external_id`` used by the exception-event bridge.

        Do not "optimise" this into ``ebay_order_id.name``: ``ebay.order`` has
        no ``name`` field, so that domain raises rather than returning nothing.
        """
        return self.search(
            ["|",
             ["ebay_order_number", "ilike", order_number],
             ["atech_ebay_external_id", "ilike", order_number]],
            limit=limit,
        )

    def pending_ai_drafts(self, limit: int = 50) -> list[dict]:
        """Tickets with an AI draft awaiting human review."""
        return self.search(
            [["ai_draft_state", "not in", [False, "none", "posted", "dismissed"]]],
            limit=limit,
        )

    # ── Writes ───────────────────────────────────────────────────────

    def draft_ai_reply(
        self, ticket_id: int, instructions: Optional[str] = None
    ) -> dict:
        """Generate an AI reply draft on a ticket — does **not** send it.

        Args:
            ticket_id: Ticket to draft on.
            instructions: Optional steer for the draft, written to
                ``ai_draft_instructions`` before generating.
        """
        if instructions:
            self.update(ticket_id, {"ai_draft_instructions": instructions})
        result = self.run_action(ticket_id, "action_ai_draft_reply")
        record = result["record"]
        state = record.get("ai_draft_state")
        err = record.get("ai_draft_error")
        return {
            "summary": (
                f"AI draft on ticket {ticket_id}: {state or 'generated'}"
                + (f" — error: {err}" if err else "")
                + ". Review it, then post explicitly."
            ),
            "draft": record.get("ai_draft"),
            "state": state,
            "error": err,
            "warning": record.get("ai_draft_warning"),
            "ticket": record,
        }

    def create_ticket(
        self,
        name: str,
        partner_id: Optional[int] = None,
        description: Optional[str] = None,
        team_id: Optional[int] = None,
        priority: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Create a helpdesk ticket."""
        values: dict[str, Any] = {"name": name}
        if partner_id:
            values["partner_id"] = partner_id
        if description:
            values["description"] = description
        if team_id:
            values["team_id"] = team_id
        if priority:
            values["priority"] = priority
        values.update(extra)
        record = self.create(values)
        return {
            "summary": f"Ticket #{record['id']} '{name}' created",
            "ticket": record,
        }

    # ── Summary ──────────────────────────────────────────────────────

    def desk_summary(self) -> dict:
        """Open counts plus the eBay deadline queue."""
        open_count = self.count([["close_date", "=", False]])
        unassigned = self.count(
            [["close_date", "=", False], ["user_id", "=", False]]
        )
        ebay_pending = self.count([["atech_ebay_action_needed", "=", True]])
        drafts = self.count(
            [["ai_draft_state", "not in", [False, "none", "posted", "dismissed"]]]
        )
        return {
            "summary": (
                f"Helpdesk: {open_count} open ({unassigned} unassigned), "
                f"{ebay_pending} eBay items need a response, "
                f"{drafts} AI drafts awaiting review"
            ),
            "open": open_count,
            "unassigned": unassigned,
            "ebay_action_needed": ebay_pending,
            "ai_drafts_pending": drafts,
        }
