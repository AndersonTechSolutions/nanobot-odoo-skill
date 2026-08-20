"""
Helpdesk operations for the ``atech_helpdesk`` module (Odoo 17 Enterprise
``helpdesk.ticket`` plus AndersonTech extensions).

The extensions add: linked repairs/RMAs/to-dos, an eBay order bridge with
exception events (``atech.helpdesk.ebay.event``) and the raw case feed
(``ebay.customer.request``), eBay buyer messaging, AI-drafted replies
(``ai_draft`` / ``ai_draft_state``), a reminder/escalation engine
(``helpdesk.reminder.rule`` firing ``helpdesk.ticket.reminder``), and product
lines carrying the device and serial a ticket is actually about
(``helpdesk.ticket.product.line``).

Two of those live on the *other* side of the relation, which is the trap:
an escalation is a ``helpdesk.ticket.reminder`` row, and a serial is on a
product line — neither is a field on the ticket. :meth:`escalated_tickets`
and :meth:`find_by_serial` resolve from those models back to tickets, because
the obvious domain on ``helpdesk.ticket`` finds nothing at all.

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

_TEAM_FIELDS = [
    "id", "name", "member_ids", "assign_method", "auto_assignment",
    "use_ai_auto_draft", "use_sla", "use_fsm", "use_product_repairs",
    "use_product_returns", "alias_email", "fsm_project_id",
]

_REMINDER_FIELDS = [
    "id", "ticket_id", "rule_id", "level", "reference_date", "user_id",
]

_PRODUCT_LINE_FIELDS = [
    "id", "ticket_id", "product_id", "lot_id", "quantity", "note", "sequence",
]

_REQUEST_FIELDS = [
    "id", "external_id", "request_kind", "case_type", "status", "reason",
    "action_needed", "action_due_date", "amount", "currency",
    "buyer_initiated", "current_open", "current_terminal", "escalated_by",
    "order_id", "legacy_order_id", "item_id", "sale_order_id",
    "creation_date", "last_seen_at",
]

#: ``helpdesk.ticket.reminder.level`` values.
REMINDER_LEVELS = ["reminder", "escalation"]

#: ``ebay.customer.request.request_kind`` values.
REQUEST_KINDS = ["inquiry", "case", "payment_dispute"]


class HelpdeskOps(BaseOps):
    """Operations on helpdesk tickets and their AndersonTech extensions."""

    MODEL = "helpdesk.ticket"
    MODULE = "atech_helpdesk"
    EVENT_MODEL = "atech.helpdesk.ebay.event"
    TEAM_MODEL = "helpdesk.team"
    REMINDER_MODEL = "helpdesk.ticket.reminder"
    RULE_MODEL = "helpdesk.reminder.rule"
    PRODUCT_LINE_MODEL = "helpdesk.ticket.product.line"
    REQUEST_MODEL = "ebay.customer.request"
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

    # ── Teams ────────────────────────────────────────────────────────

    def teams(self) -> list[dict]:
        """Helpdesk teams with the AndersonTech feature flags that matter.

        ``use_ai_auto_draft`` is the one worth checking when AI drafts are
        appearing (or not appearing) on tickets — it is a per-team switch, not
        a global one, so a quiet team is usually a team with the flag off.
        """
        self._require()
        rows = self.client.search_read(
            self.TEAM_MODEL, [["active", "=", True]],
            fields=_TEAM_FIELDS, order="sequence, id",
        )
        for row in rows:
            row["member_count"] = len(row.pop("member_ids", []) or [])
        return rows

    def team_load(self) -> dict:
        """Open ticket counts per team, with the unassigned share.

        Counted here rather than read from ``helpdesk.team.open_ticket_count``:
        that field is computed and unstored, and the unassigned split — the
        number that actually says whether a team is coping — is not on the
        team model at all.
        """
        self._require()
        teams = self.client.search_read(
            self.TEAM_MODEL, [["active", "=", True]], fields=["id", "name"],
            order="sequence, id",
        )
        out = []
        for team in teams:
            open_count = self.count(
                [["team_id", "=", team["id"]], ["close_date", "=", False]]
            )
            unassigned = self.count(
                [["team_id", "=", team["id"]], ["close_date", "=", False],
                 ["user_id", "=", False]]
            )
            out.append({
                "team_id": team["id"], "team": team["name"],
                "open": open_count, "unassigned": unassigned,
            })
        busiest = max(out, key=lambda r: r["open"], default=None)
        return {
            "summary": (
                "Team load: "
                + ", ".join(f"{r['team']} {r['open']} open "
                            f"({r['unassigned']} unassigned)" for r in out)
                if out else "No active helpdesk teams."
            ),
            "busiest": busiest,
            "teams": out,
        }

    def tickets_for_team(self, team_id: int, limit: int = 50) -> list[dict]:
        """Open tickets belonging to one team."""
        return self.search(
            [["team_id", "=", team_id], ["close_date", "=", False]], limit=limit
        )

    # ── Reminders and escalations ────────────────────────────────────

    def reminders(
        self, level: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """Fired reminders/escalations, newest first.

        Args:
            level: ``reminder`` or ``escalation``; omit for both.
        """
        self._require()
        if level and level not in REMINDER_LEVELS:
            raise ValueError(
                f"level must be one of {REMINDER_LEVELS}, got {level!r}"
            )
        domain = [["level", "=", level]] if level else []
        return self.client.search_read(
            self.REMINDER_MODEL, domain, fields=_REMINDER_FIELDS,
            limit=limit, order="reference_date desc, id desc",
        )

    def escalated_tickets(self, limit: int = 50) -> list[dict]:
        """Open tickets that have fired an escalation.

        The escalation lives on ``helpdesk.ticket.reminder``, not on the
        ticket, so this resolves the reminders first and reads the tickets
        they point at — a domain on the ticket side would find nothing.
        """
        ticket_ids = self._escalated_ticket_ids()
        if not ticket_ids:
            return []
        return self.search(
            [["id", "in", ticket_ids], ["close_date", "=", False]], limit=limit
        )

    def _escalated_ticket_ids(self) -> list[int]:
        """Distinct ticket ids that have fired an escalation.

        Bounded by ``COMPUTED_SCAN_CAP`` reminder rows, and it says so when it
        hits the cap — an escalation older than that window is not reported.
        Shared by :meth:`escalated_tickets` and :meth:`desk_summary` so the
        list and the count cannot disagree.
        """
        self._require()
        rows = self.client.search_read(
            self.REMINDER_MODEL, [["level", "=", "escalation"]],
            fields=["ticket_id"], limit=self.COMPUTED_SCAN_CAP,
            order="reference_date desc",
        )
        if len(rows) >= self.COMPUTED_SCAN_CAP:
            logger.warning(
                "helpdesk.ticket.reminder: escalation scan hit the %d-row cap; "
                "older escalations are not reported.", self.COMPUTED_SCAN_CAP,
            )
        ticket_ids: list[int] = []
        for row in rows:
            ref = row.get("ticket_id")
            tid = ref[0] if isinstance(ref, (list, tuple)) else ref
            if tid and tid not in ticket_ids:
                ticket_ids.append(tid)
        return ticket_ids

    def reminder_rules(self) -> list[dict]:
        """The rules that drive reminders and escalations."""
        self._require()
        return self.client.search_read(
            self.RULE_MODEL, [], fields=["id", "display_name"], order="id",
        )

    # ── Ticket product lines ─────────────────────────────────────────

    def get_products(self, ticket_id: int) -> list[dict]:
        """Devices/products attached to a ticket, with their serials."""
        self._require()
        return self.client.search_read(
            self.PRODUCT_LINE_MODEL, [["ticket_id", "=", ticket_id]],
            fields=_PRODUCT_LINE_FIELDS, order="sequence, id",
        )

    def add_product(
        self, ticket_id: int, product_id: int, quantity: float = 1.0,
        lot_id: Optional[int] = None, note: Optional[str] = None,
    ) -> dict:
        """Attach a product (and optionally its serial) to a ticket.

        This is what makes a ticket actionable for a return or a repair —
        ``use_product_returns`` / ``use_product_repairs`` on the team drive off
        these lines.
        """
        self._require()
        values: dict[str, Any] = {
            "ticket_id": ticket_id,
            "product_id": product_id,
            "quantity": quantity,
        }
        if lot_id:
            values["lot_id"] = lot_id
        if note:
            values["note"] = note
        line_id = self.client.create(self.PRODUCT_LINE_MODEL, values)
        return {
            "summary": f"Product line #{line_id} added to ticket {ticket_id}",
            "line_id": line_id,
            "products": self.get_products(ticket_id),
        }

    def find_by_serial(self, serial: str, limit: int = 20) -> list[dict]:
        """Find tickets whose attached product carries a serial/lot.

        Resolves through the product lines rather than the ticket, since the
        serial is only ever recorded on the line.
        """
        self._require()
        lines = self.client.search_read(
            self.PRODUCT_LINE_MODEL, [["lot_id.name", "ilike", serial]],
            fields=["ticket_id"], limit=self.COMPUTED_SCAN_CAP,
        )
        ticket_ids = []
        for line in lines:
            ref = line.get("ticket_id")
            tid = ref[0] if isinstance(ref, (list, tuple)) else ref
            if tid and tid not in ticket_ids:
                ticket_ids.append(tid)
        if not ticket_ids:
            return []
        return self.search([["id", "in", ticket_ids]], limit=limit)

    # ── eBay customer requests (cases / disputes) ────────────────────

    def ebay_requests(
        self, kind: Optional[str] = None, open_only: bool = True,
        limit: int = 50,
    ) -> list[dict]:
        """eBay inquiries, cases and payment disputes.

        Distinct from :meth:`ebay_action_needed`, which reads the ticket side.
        This is the raw case feed — a payment dispute exists here whether or
        not anyone has raised a ticket for it.

        Args:
            kind: One of :data:`REQUEST_KINDS`; omit for all.
            open_only: Restrict to cases still open (default).
            limit: Maximum rows.
        """
        self._require()
        if kind and kind not in REQUEST_KINDS:
            raise ValueError(
                f"kind must be one of {REQUEST_KINDS}, got {kind!r}"
            )
        domain: list = []
        if kind:
            domain.append(["request_kind", "=", kind])
        if open_only:
            domain.append(["current_open", "=", True])
        return self.client.search_read(
            self.REQUEST_MODEL, domain, fields=_REQUEST_FIELDS,
            limit=limit, order="action_due_date asc, id desc",
        )

    def ebay_requests_due(self, limit: int = 50) -> list[dict]:
        """eBay cases with a response deadline, soonest first.

        Missing an eBay case deadline forfeits the case automatically, so this
        outranks everything else in the desk queue.
        """
        self._require()
        return self.client.search_read(
            self.REQUEST_MODEL,
            [["action_needed", "=", True], ["current_terminal", "=", False]],
            fields=_REQUEST_FIELDS, limit=limit, order="action_due_date asc",
        )

    def payment_disputes(self, limit: int = 50) -> list[dict]:
        """Open eBay payment disputes — money already taken back or at risk."""
        self._require()
        return self.client.search_read(
            self.REQUEST_MODEL,
            [["request_kind", "=", "payment_dispute"],
             ["current_terminal", "=", False]],
            fields=_REQUEST_FIELDS, limit=limit, order="action_due_date asc",
        )

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
        try:
            cases_due = self.client.search_count(
                self.REQUEST_MODEL,
                [["action_needed", "=", True], ["current_terminal", "=", False]],
            )
            disputes = self.client.search_count(
                self.REQUEST_MODEL,
                [["request_kind", "=", "payment_dispute"],
                 ["current_terminal", "=", False]],
            )
        except Exception:  # noqa: BLE001 — a summary must not fail on one count
            cases_due = disputes = -1
        esc_ids = self._escalated_ticket_ids()
        escalated = self.count(
            [["id", "in", esc_ids], ["close_date", "=", False]]
        ) if esc_ids else 0
        return {
            "summary": (
                f"Helpdesk: {open_count} open ({unassigned} unassigned), "
                f"{ebay_pending} eBay items need a response, "
                f"{drafts} AI drafts awaiting review, {escalated} escalated"
                + (f", {cases_due} eBay cases past due" if cases_due > 0 else "")
                + (f", {disputes} open payment disputes" if disputes > 0 else "")
            ),
            "open": open_count,
            "unassigned": unassigned,
            "ebay_action_needed": ebay_pending,
            "ai_drafts_pending": drafts,
            "escalated": escalated,
            "ebay_cases_due": cases_due,
            "payment_disputes": disputes,
        }
