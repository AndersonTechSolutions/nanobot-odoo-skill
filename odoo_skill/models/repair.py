"""
Repair operations for the ``atech_repair`` module (Odoo 17 ``repair.order``).

``atech_repair`` extends Odoo's native ``repair.order`` with intake, device
specs, warranty lookup, technician timers, checklists, customer updates, and
a pickup/checkout flow.

Two things shape this class:

* ``state`` is **readonly** (computed from ``stage_id`` via ``stage_state_map``).
  Transitions therefore go through button methods — never a write to ``state``.
* Creating a repair order requires ``location_id``, ``picking_type_id``,
  ``recycle_location_id``, ``repair_type`` and ``schedule_date``. Rather than
  make a caller supply warehouse plumbing it will not know, :meth:`create_repair`
  fills those from the most recent existing repair order (see
  :meth:`_warehouse_defaults`).
"""

import logging
from typing import Any, Optional

from ._base import BaseOps, summarize

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "partner_id", "device_display", "repair_type",
    "state", "stage_id", "priority", "schedule_date", "promised_date",
    "warranty_status", "is_overdue", "is_awaiting_parts", "user_id",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "partner_email", "partner_phone", "reported_problem", "repair_request",
    "diagnosis_summary", "internal_notes", "device_brand_id", "device_model",
    "spec_cpu", "spec_ram", "spec_storage", "spec_storage_type",
    "scanned_serial", "lot_id", "under_warranty", "warranty_end_date",
    "parts_availability", "parts_all_received", "total_hours",
    "timer_running", "sale_order_id", "ticket_id", "status_url",
    "picked_up_by", "pickup_signed_on", "ready_notified_on", "tag_ids",
]

#: state values, in workflow order (computed from stage_id).
STATES = ["draft", "confirmed", "under_repair", "done", "cancel"]

#: repair_type values.
REPAIR_TYPES = ["customer", "internal", "refurb"]


class RepairOps(BaseOps):
    """Workflow operations on ``repair.order``."""

    MODEL = "repair.order"
    MODULE = "atech_repair"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "priority desc, schedule_date asc"

    ALLOWED_ACTIONS = frozenset({
        # lifecycle
        "action_check_in_device",
        "action_repair_start",
        "action_repair_end",
        "action_mark_ready_pickup",
        "action_repair_cancel",
        "action_repair_cancel_draft",
        "action_validate",
        "action_assign",
        # technician timers
        "action_timer_start",
        "action_timer_pause",
        "action_timer_stop",
        # customer comms
        "action_post_customer_update",
        "action_post_repair_note",
        "action_email_received",
        "action_email_diagnosis",
        "action_email_update",
        "action_email_review",
        "action_email_dropoff_receipt",
        # quoting / parts
        "action_quote_from_diagnosis",
        "action_create_sale_order",
        "action_load_checklist",
        "action_parts_back_to_bench",
        # downstream
        "action_schedule_fsm_job",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def open_repairs(self, limit: int = 50) -> list[dict]:
        """Repairs not yet finished or cancelled."""
        return self.search([["state", "not in", ["done", "cancel"]]], limit=limit)

    def repairs_for_customer(self, partner_id: int, limit: int = 50) -> list[dict]:
        """All repairs for a customer, newest first."""
        return self.search(
            [["partner_id", "=", partner_id]], limit=limit, order="id desc"
        )

    def overdue_repairs(self, limit: int = 50) -> list[dict]:
        """Repairs flagged overdue against their promised date.

        ``is_overdue`` is computed and not stored, so it is filtered
        client-side over the open-repair set (see
        :meth:`BaseOps.search_computed`).
        """
        return self.search_computed(
            [["state", "not in", ["done", "cancel"]]],
            lambda r: bool(r.get("is_overdue")),
            limit=limit, extra_fields=["is_overdue"],
        )

    def awaiting_parts(self, limit: int = 50) -> list[dict]:
        """Repairs blocked waiting on parts.

        ``is_awaiting_parts`` is computed and not stored — filtered
        client-side over the open-repair set.
        """
        return self.search_computed(
            [["state", "not in", ["done", "cancel"]]],
            lambda r: bool(r.get("is_awaiting_parts")),
            limit=limit, extra_fields=["is_awaiting_parts"],
        )

    def find_by_serial(self, serial: str, limit: int = 10) -> list[dict]:
        """Locate repairs by scanned serial or attached lot."""
        return self.search(
            ["|", ["scanned_serial", "ilike", serial],
                  ["lot_id.name", "ilike", serial]],
            limit=limit,
        )

    def bench_summary(self) -> dict:
        """Counts by state plus the two blocking buckets — a standup view."""
        counts = {s: self.count([["state", "=", s]]) for s in STATES}
        open_domain = [["state", "not in", ["done", "cancel"]]]
        overdue = self.count_computed(
            open_domain, lambda r: bool(r.get("is_overdue")),
            extra_fields=["is_overdue"],
        )
        parts = self.count_computed(
            open_domain, lambda r: bool(r.get("is_awaiting_parts")),
            extra_fields=["is_awaiting_parts"],
        )
        active = sum(counts[s] for s in ("draft", "confirmed", "under_repair"))
        return {
            "summary": (
                f"Bench: {active} active "
                f"({counts['under_repair']} on bench, {counts['confirmed']} queued, "
                f"{counts['draft']} draft), {overdue} overdue, {parts} awaiting parts"
            ),
            "by_state": counts,
            "overdue": overdue,
            "awaiting_parts": parts,
            "active": active,
        }

    # ── Writes ───────────────────────────────────────────────────────

    def _warehouse_defaults(self) -> dict:
        """Borrow required warehouse fields from the newest existing repair.

        ``repair.order`` requires ``location_id``, ``picking_type_id`` and
        ``recycle_location_id``. These are per-company warehouse plumbing that
        a chat caller has no way to know, and Odoo's own defaults do not fire
        over XML-RPC the way they do in the UI. Copying them from the most
        recent order keeps new records consistent with the shop's setup.

        Returns an empty dict when there is no prior order, in which case the
        caller must supply the fields explicitly.
        """
        prior = self.client.search_read(
            self.MODEL, [],
            fields=["location_id", "picking_type_id", "recycle_location_id"],
            limit=1, order="id desc",
        )
        if not prior:
            return {}
        row = prior[0]
        out = {}
        for f in ("location_id", "picking_type_id", "recycle_location_id"):
            val = row.get(f)
            if val:
                out[f] = val[0] if isinstance(val, (list, tuple)) else val
        return out

    def create_repair(
        self,
        partner_id: int,
        reported_problem: str,
        repair_type: str = "customer",
        schedule_date: Optional[str] = None,
        device_model: Optional[str] = None,
        scanned_serial: Optional[str] = None,
        promised_date: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Create a repair order (intake).

        Args:
            partner_id: Customer ``res.partner`` id.
            reported_problem: What the customer says is wrong.
            repair_type: One of :data:`REPAIR_TYPES`.
            schedule_date: Required by the model; defaults to now.
            device_model: Free-text device model.
            scanned_serial: Serial captured at intake.
            promised_date: Customer-facing promise date.
            **extra: Any other ``repair.order`` field.

        Returns:
            The created repair order in detail form.
        """
        if repair_type not in REPAIR_TYPES:
            raise ValueError(
                f"repair_type must be one of {REPAIR_TYPES}, got {repair_type!r}"
            )

        values: dict[str, Any] = self._warehouse_defaults()
        values.update({
            "partner_id": partner_id,
            "reported_problem": reported_problem,
            "repair_type": repair_type,
        })
        if schedule_date:
            values["schedule_date"] = schedule_date
        else:
            from datetime import datetime
            values["schedule_date"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if device_model:
            values["device_model"] = device_model
        if scanned_serial:
            values["scanned_serial"] = scanned_serial
        if promised_date:
            values["promised_date"] = promised_date
        values.update(extra)

        record = self.create(values)
        return {
            "summary": f"Repair {record['name']} created for "
                       f"{_name_of(record.get('partner_id'))} ({repair_type})",
            "repair": record,
        }

    def post_customer_update(self, repair_id: int, message: str) -> dict:
        """Draft and post a customer-facing update on a repair.

        ``action_post_customer_update`` publishes whatever sits in
        ``customer_update_draft``, so the text is written first.
        """
        self.update(repair_id, {"customer_update_draft": message})
        return self.run_action(repair_id, "action_post_customer_update")

    def set_diagnosis(self, repair_id: int, diagnosis: str) -> dict:
        """Record the technician's diagnosis summary."""
        record = self.update(repair_id, {"diagnosis_summary": diagnosis})
        return {
            "summary": f"Diagnosis recorded on {record['name']}",
            "repair": record,
        }


def _name_of(value: Any) -> str:
    """Render a many2one ``[id, name]`` pair as its name."""
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    return str(value) if value else "—"
