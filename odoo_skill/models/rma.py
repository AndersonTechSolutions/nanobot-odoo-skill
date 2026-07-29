"""
RMA operations for the ``atech_rma`` module (Odoo 17 ``rma.order``).

Covers the return-authorisation lifecycle: intake, approval, receiving,
resolution execution (refund / replace / repair), rejection, and the eBay
return bridge.

Unlike ``repair.order``, ``rma.order.state`` is a writable selection — but
writing it directly skips the module's side effects (emails, pickings,
resolution execution). Every transition here goes through a button method.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "partner_id", "state", "channel", "return_method",
    "coverage_type", "priority", "date_request", "subject", "user_id",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "reason_id", "line_ids", "sale_order_id", "helpdesk_ticket_id",
    "return_tracking_number", "return_carrier", "return_picking_id",
    "receipt_validated", "return_instructions_sent", "reject_reason_id",
    "reject_note", "is_oem_claim", "warranty_denied_on",
    "ebay_item_id", "ebay_return_id", "ebay_return_state",
    "ebay_return_reason", "ebay_buyer_comment", "ebay_escalated",
    "can_execute_resolutions", "has_advance_return_due",
    "advance_return_overdue", "storage_location_id", "picked_up_by",
]

_LINE_FIELDS = [
    "id", "rma_id", "product_id", "quantity", "resolution",
    "warranty_status", "warranty_source", "rma_state",
]

#: rma.order.state values in workflow order.
STATES = ["draft", "submitted", "approved", "processing", "done",
          "rejected", "cancelled"]

#: How the item comes back.
RETURN_METHODS = ["ship", "dropoff", "in_hand", "advance_replacement",
                  "refund_no_return"]

#: Per-line resolution.
RESOLUTIONS = ["refund", "replace", "replace_advance", "replace_no_return",
               "repair"]

#: Where the RMA originated.
CHANNELS = ["direct", "ebay", "helpdesk"]


class RMAOps(BaseOps):
    """Workflow operations on ``rma.order``."""

    MODEL = "rma.order"
    MODULE = "atech_rma"
    LINE_MODEL = "rma.order.line"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "priority desc, date_request desc"

    ALLOWED_ACTIONS = frozenset({
        # lifecycle
        "action_submit",
        "action_approve",
        "action_reject",
        "action_cancel",
        "action_reset_to_draft",
        "action_execute_resolutions",
        # receiving / logistics
        "action_send_return_instructions",
        "action_buy_return_label",
        "action_mark_oem_replacement_received",
        "action_mark_warranty_denied",
        # advance replacement
        "action_advance_charge_non_return",
        "action_advance_waive_non_return",
        # customer comms
        "action_post_customer_update",
        "action_email_rma_created",
        "action_email_return_instructions",
        "action_email_ready",
        "action_email_completed",
        "action_email_rejected",
        "action_email_dropoff",
        "action_email_advance_replacement",
        "action_email_refund_no_return",
        "action_email_replacement_no_return",
        "action_email_oem_received",
        "action_email_denied_manufacturer",
        # eBay bridge
        "action_ebay_reply_buyer",
        "action_report_problem_ebay",
        "action_offer_partial_refund",
        # downstream
        "action_schedule_fsm_job",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def open_rmas(self, limit: int = 50) -> list[dict]:
        """RMAs still in flight."""
        return self.search(
            [["state", "not in", ["done", "rejected", "cancelled"]]], limit=limit
        )

    def awaiting_approval(self, limit: int = 50) -> list[dict]:
        """Submitted RMAs waiting on a decision."""
        return self.search([["state", "=", "submitted"]], limit=limit)

    def ready_to_execute(self, limit: int = 50) -> list[dict]:
        """RMAs whose resolutions can now be executed.

        ``can_execute_resolutions`` is computed and not stored, so it is
        filtered client-side over the open-RMA set.
        """
        return self.search_computed(
            [["state", "not in", ["done", "rejected", "cancelled"]]],
            lambda r: bool(r.get("can_execute_resolutions")),
            limit=limit, extra_fields=["can_execute_resolutions"],
        )

    def overdue_advance_returns(self, limit: int = 50) -> list[dict]:
        """Advance replacements where the old unit never came back.

        ``advance_return_overdue`` is computed and not stored; the scan is
        narrowed to advance-replacement RMAs before filtering client-side.
        """
        return self.search_computed(
            [["return_method", "=", "advance_replacement"]],
            lambda r: bool(r.get("advance_return_overdue")),
            limit=limit, extra_fields=["advance_return_overdue"],
        )

    def ebay_rmas(self, limit: int = 50) -> list[dict]:
        """RMAs originating from eBay returns."""
        return self.search([["channel", "=", "ebay"]], limit=limit)

    def escalated_ebay(self, limit: int = 50) -> list[dict]:
        """eBay returns escalated to a case."""
        return self.search([["ebay_escalated", "=", True]], limit=limit)

    def get_lines(self, rma_id: int) -> list[dict]:
        """Read the line items on an RMA."""
        self._require()
        return self.client.search_read(
            self.LINE_MODEL, [["rma_id", "=", rma_id]], fields=_LINE_FIELDS, limit=200
        )

    def get_with_lines(self, rma_id: int) -> dict:
        """Detail view plus resolved line items."""
        record = self.get(rma_id)
        record["lines"] = self.get_lines(rma_id)
        return record

    def pipeline_summary(self) -> dict:
        """Counts by state — the RMA desk's morning view."""
        counts = {s: self.count([["state", "=", s]]) for s in STATES}
        open_count = sum(
            counts[s] for s in ("draft", "submitted", "approved", "processing")
        )
        overdue = self.count_computed(
            [["return_method", "=", "advance_replacement"]],
            lambda r: bool(r.get("advance_return_overdue")),
            extra_fields=["advance_return_overdue"],
        )
        return {
            "summary": (
                f"RMA pipeline: {open_count} open "
                f"({counts['submitted']} awaiting approval, "
                f"{counts['processing']} processing), "
                f"{overdue} overdue advance returns"
            ),
            "by_state": counts,
            "open": open_count,
            "overdue_advance_returns": overdue,
        }

    # ── Writes ───────────────────────────────────────────────────────

    def create_rma(
        self,
        partner_id: int,
        return_method: str = "ship",
        subject: Optional[str] = None,
        channel: str = "direct",
        lines: Optional[list[dict]] = None,
        reason_id: Optional[int] = None,
        **extra: Any,
    ) -> dict:
        """Create an RMA, optionally with line items.

        Args:
            partner_id: Customer ``res.partner`` id.
            return_method: One of :data:`RETURN_METHODS`.
            subject: Short description of the return.
            channel: One of :data:`CHANNELS`.
            lines: ``[{"product_id": int, "quantity": float,
                      "resolution": str}, ...]``.
            reason_id: ``rma.reason`` id.
            **extra: Any other ``rma.order`` field.
        """
        if return_method not in RETURN_METHODS:
            raise ValueError(
                f"return_method must be one of {RETURN_METHODS}, got {return_method!r}"
            )
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {CHANNELS}, got {channel!r}")

        values: dict[str, Any] = {
            "partner_id": partner_id,
            "return_method": return_method,
            "channel": channel,
        }
        if subject:
            values["subject"] = subject
        if reason_id:
            values["reason_id"] = reason_id
        if lines:
            values["line_ids"] = [
                (0, 0, _line_vals(ln)) for ln in lines
            ]
        values.update(extra)

        record = self.create(values)
        return {
            "summary": f"RMA {record['name']} created for "
                       f"{_name_of(record.get('partner_id'))} "
                       f"({return_method}, {len(lines or [])} line(s))",
            "rma": record,
        }

    def add_line(
        self,
        rma_id: int,
        product_id: int,
        quantity: float = 1.0,
        resolution: str = "refund",
    ) -> dict:
        """Append a line item to an existing RMA."""
        if resolution not in RESOLUTIONS:
            raise ValueError(
                f"resolution must be one of {RESOLUTIONS}, got {resolution!r}"
            )
        self._require()
        line_id = self.client.create(self.LINE_MODEL, {
            "rma_id": rma_id,
            "product_id": product_id,
            "quantity": quantity,
            "resolution": resolution,
        })
        return {
            "summary": f"Added line {line_id} ({resolution} × {quantity}) to RMA {rma_id}",
            "line_id": line_id,
            "lines": self.get_lines(rma_id),
        }

    def set_line_resolution(self, line_id: int, resolution: str) -> dict:
        """Change one line's resolution before executing."""
        if resolution not in RESOLUTIONS:
            raise ValueError(
                f"resolution must be one of {RESOLUTIONS}, got {resolution!r}"
            )
        self._require()
        self.client.write(self.LINE_MODEL, line_id, {"resolution": resolution})
        rows = self.client.read(self.LINE_MODEL, [line_id], fields=_LINE_FIELDS)
        return {"summary": f"Line {line_id} set to {resolution}", "line": rows[0]}

    def post_customer_update(self, rma_id: int, message: str) -> dict:
        """Draft and post a customer-facing update on an RMA."""
        self.update(rma_id, {"customer_update_draft": message})
        return self.run_action(rma_id, "action_post_customer_update")

    def record_tracking(
        self, rma_id: int, tracking_number: str, carrier: Optional[str] = None
    ) -> dict:
        """Record the customer's inbound return tracking."""
        values: dict[str, Any] = {"return_tracking_number": tracking_number}
        if carrier:
            values["return_carrier"] = carrier
        record = self.update(rma_id, values)
        return {
            "summary": f"Tracking {tracking_number} recorded on {record['name']}",
            "rma": record,
        }


def _line_vals(line: dict) -> dict:
    """Normalise a caller-supplied line dict into Odoo create values."""
    resolution = line.get("resolution", "refund")
    if resolution not in RESOLUTIONS:
        raise ValueError(
            f"resolution must be one of {RESOLUTIONS}, got {resolution!r}"
        )
    vals = {
        "product_id": line["product_id"],
        "quantity": float(line.get("quantity", 1.0)),
        "resolution": resolution,
    }
    for passthrough in ("lot_id", "note", "sale_order_line_id"):
        if passthrough in line:
            vals[passthrough] = line[passthrough]
    return vals


def _name_of(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    return str(value) if value else "—"
