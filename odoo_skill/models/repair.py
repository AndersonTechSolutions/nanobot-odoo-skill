"""
Repair operations for the ``atech_repair`` module (Odoo 17 ``repair.order``).

``atech_repair`` extends Odoo's native ``repair.order`` with intake, device
specs, warranty lookup, technician timers, checklists, customer updates, and
a pickup/checkout flow.

Four things shape this class:

* ``state`` is **readonly** (computed from ``stage_id`` via ``stage_state_map``).
  Transitions therefore go through button methods — never a write to ``state``.
  :meth:`RepairOps.move_stage` is the supported way to move a ticket across
  the board, and :meth:`stages` shows which native state each stage maps to.
* Creating a repair order requires ``location_id``, ``picking_type_id``,
  ``recycle_location_id``, ``repair_type`` and ``schedule_date``. Rather than
  make a caller supply warehouse plumbing it will not know, :meth:`create_repair`
  fills those from the most recent existing repair order (see
  :meth:`_warehouse_defaults`).
* **Part receipt is not writable here.** ``repair.part.line.qty_received`` and
  its ``state`` roll up from stock moves, so :meth:`add_part` records what the
  bench needs and receiving happens in inventory. Both are unstored, so
  :meth:`outstanding_parts` filters client-side.
* **Time logs need a technician and a date** — the model requires both, so
  :meth:`log_time` defaults them to the API user and today rather than
  failing on plumbing an unattended caller cannot know.

Beyond ``repair.order`` itself this class reaches the module's sub-models:
parts (``repair.part.line``), labour (``repair.time.log``), QC checklists
(``repair.order.checklist.line``), depot/OEM shipments
(``repair.oem.shipment``), board stages (``repair.stage``) and multi-device
drop-offs (``repair.intake.batch``).
"""

import logging
from typing import Any, Optional

from ._base import BaseOps, summarize, utc_stamp

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

#: repair.order.checklist.line state values.
CHECK_STATES = ["todo", "pass", "fail", "na", "not_tested"]

#: repair.oem.shipment carrier values.
OEM_CARRIERS = ["ups", "fedex", "usps", "dhl", "other"]

_PART_FIELDS = [
    "id", "repair_order_id", "product_id", "qty_needed", "qty_received",
    "state", "note", "sequence",
]

_TIME_FIELDS = ["id", "order_id", "user_id", "date", "hours", "note"]

_CHECKLIST_FIELDS = ["id", "order_id", "name", "state", "note", "sequence"]

_OEM_FIELDS = [
    "id", "order_id", "name", "carrier", "tracking_ref", "tracking_url",
    "shipped_date", "received_date", "note",
]

_STAGE_FIELDS = ["id", "name", "sequence", "state_map", "fold", "is_closed"]

_BATCH_FIELDS = [
    "id", "name", "partner_id", "partner_email", "partner_phone",
    "repair_type", "priority", "promised_date", "device_in_hand",
    "collected_from_customer", "dropoff_signed_on", "storage_location_id",
    "order_count",
]


class RepairOps(BaseOps):
    """Workflow operations on ``repair.order``."""

    MODEL = "repair.order"
    MODULE = "atech_repair"
    PART_MODEL = "repair.part.line"
    TIME_MODEL = "repair.time.log"
    CHECKLIST_MODEL = "repair.order.checklist.line"
    OEM_MODEL = "repair.oem.shipment"
    STAGE_MODEL = "repair.stage"
    BATCH_MODEL = "repair.intake.batch"
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
            values["schedule_date"] = utc_stamp()
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

    # ── Parts ────────────────────────────────────────────────────────
    #
    # ``repair.part.line`` is the parts-needed list for a bench ticket. Its
    # ``state`` (needed / partial / received) and ``qty_received`` are both
    # computed and unstored — they roll up from the stock moves, not from
    # anything writable here — so parts queries filter client-side and
    # receiving a part is a stock operation, not a write to this model.

    def get_parts(self, repair_id: int) -> list[dict]:
        """Parts required for a repair, with their received status."""
        self._require()
        return self.client.search_read(
            self.PART_MODEL, [["repair_order_id", "=", repair_id]],
            fields=_PART_FIELDS, order="sequence, id",
        )

    def outstanding_parts(self, limit: int = 100) -> list[dict]:
        """Parts still owed across every open repair, oldest ticket first.

        ``state`` on ``repair.part.line`` is computed and unstored, so the
        scan is narrowed by the stored link to open repairs and the
        needed/partial test runs client-side.
        """
        self._require()
        window = min(max(limit * self.COMPUTED_SCAN_FACTOR, 200),
                     self.COMPUTED_SCAN_CAP)
        rows = self.client.search_read(
            self.PART_MODEL,
            [["repair_order_id.state", "not in", ["done", "cancel"]]],
            fields=_PART_FIELDS, limit=window, order="repair_order_id, sequence",
        )
        if len(rows) >= window:
            logger.warning(
                "repair.part.line: outstanding_parts scanned the full %d-row "
                "window; results may be incomplete.", window,
            )
        return [r for r in rows if r.get("state") in ("needed", "partial")][:limit]

    def add_part(
        self, repair_id: int, product_id: int, qty_needed: float = 1.0,
        note: Optional[str] = None,
    ) -> dict:
        """Add a required part to a repair ticket.

        Records what the bench *needs*. Receiving it is a stock movement —
        ``qty_received`` and the line ``state`` are computed from the moves
        and cannot be written here.
        """
        self._require()
        values: dict[str, Any] = {
            "repair_order_id": repair_id,
            "product_id": product_id,
            "qty_needed": qty_needed,
        }
        if note:
            values["note"] = note
        line_id = self.client.create(self.PART_MODEL, values)
        return {
            "summary": f"Part line #{line_id} added to repair {repair_id}",
            "line_id": line_id,
            "parts": self.get_parts(repair_id),
        }

    # ── Technician time ──────────────────────────────────────────────

    def get_time_logs(self, repair_id: int) -> list[dict]:
        """Labour logged against a repair."""
        self._require()
        return self.client.search_read(
            self.TIME_MODEL, [["order_id", "=", repair_id]],
            fields=_TIME_FIELDS, order="date desc, id desc",
        )

    def log_time(
        self, repair_id: int, hours: float, note: Optional[str] = None,
        user_id: Optional[int] = None, date: Optional[str] = None,
    ) -> dict:
        """Log bench time against a repair.

        The model requires ``date``, ``hours``, ``user_id`` and ``order_id``,
        so the technician defaults to the API user and the date to today —
        an unattended caller has no other sensible answer for either.

        Args:
            repair_id: Repair to bill the time to.
            hours: Hours worked.
            note: What was done.
            user_id: Technician; defaults to the API user.
            date: ``YYYY-MM-DD``; defaults to today.
        """
        if hours is None or float(hours) <= 0:
            raise ValueError("hours must be positive")
        self._require()
        from datetime import date as _date
        values: dict[str, Any] = {
            "order_id": repair_id,
            "hours": float(hours),
            "user_id": user_id or self.client.uid,
            "date": date or _date.today().strftime("%Y-%m-%d"),
        }
        if note:
            values["note"] = note
        log_id = self.client.create(self.TIME_MODEL, values)
        record = self.get(repair_id)
        return {
            "summary": (
                f"{hours}h logged on {record['name']} "
                f"(total now {record.get('total_hours')}h)"
            ),
            "log_id": log_id,
            "total_hours": record.get("total_hours"),
        }

    def time_by_technician(self, repair_id: int) -> dict:
        """Labour on a repair, totalled per technician."""
        logs = self.get_time_logs(repair_id)
        totals: dict[str, float] = {}
        for log in logs:
            who = _name_of(log.get("user_id"))
            totals[who] = totals.get(who, 0.0) + (log.get("hours") or 0.0)
        overall = round(sum(totals.values()), 2)
        return {
            "summary": (
                f"{overall}h across {len(totals)} technician(s): "
                + ", ".join(f"{k} {round(v, 2)}h" for k, v in totals.items())
                if totals else "No time logged."
            ),
            "total_hours": overall,
            "by_technician": {k: round(v, 2) for k, v in totals.items()},
            "logs": logs,
        }

    # ── Checklists ───────────────────────────────────────────────────

    def get_checklist(self, repair_id: int) -> dict:
        """The QC checklist on a repair, with its pass/fail tally."""
        self._require()
        rows = self.client.search_read(
            self.CHECKLIST_MODEL, [["order_id", "=", repair_id]],
            fields=_CHECKLIST_FIELDS, order="sequence, id",
        )
        tally: dict[str, int] = {}
        for row in rows:
            tally[row["state"]] = tally.get(row["state"], 0) + 1
        failed = [r for r in rows if r["state"] == "fail"]
        return {
            "summary": (
                f"{len(rows)} check(s): "
                + ", ".join(f"{v} {k}" for k, v in sorted(tally.items()))
                + (f" — {len(failed)} FAILED" if failed else "")
                if rows else "No checklist loaded on this repair."
            ),
            "tally": tally,
            "failed": failed,
            "checks": rows,
        }

    def set_check(
        self, check_id: int, state: str, note: Optional[str] = None
    ) -> dict:
        """Record the outcome of one checklist item.

        Args:
            check_id: ``repair.order.checklist.line`` id.
            state: One of :data:`CHECK_STATES`.
            note: Optional detail — worth filling in on a ``fail``.
        """
        if state not in CHECK_STATES:
            raise ValueError(
                f"state must be one of {CHECK_STATES}, got {state!r}"
            )
        self._require()
        values: dict[str, Any] = {"state": state}
        if note:
            values["note"] = note
        self.client.write(self.CHECKLIST_MODEL, check_id, values)
        rows = self.client.read(
            self.CHECKLIST_MODEL, [check_id], fields=_CHECKLIST_FIELDS
        )
        row = rows[0] if rows else {}
        return {
            "summary": f"Check '{row.get('name')}' marked {state}",
            "check": row,
        }

    def failed_checks(self, limit: int = 100) -> list[dict]:
        """Failed QC checks across open repairs — the rework queue."""
        self._require()
        return self.client.search_read(
            self.CHECKLIST_MODEL,
            [["state", "=", "fail"],
             ["order_id.state", "not in", ["done", "cancel"]]],
            fields=_CHECKLIST_FIELDS, limit=limit, order="order_id, sequence",
        )

    # ── OEM / depot shipments ────────────────────────────────────────

    def get_oem_shipments(self, repair_id: int) -> list[dict]:
        """Parts or units sent out to a manufacturer/depot for this repair."""
        self._require()
        return self.client.search_read(
            self.OEM_MODEL, [["order_id", "=", repair_id]],
            fields=_OEM_FIELDS, order="shipped_date desc, id desc",
        )

    def outstanding_oem_shipments(self, limit: int = 50) -> list[dict]:
        """Anything sent to an OEM and not yet back.

        These are the longest poles on a repair — a unit at a depot can sit
        for weeks — and nothing else in the module surfaces them as a queue.
        """
        self._require()
        return self.client.search_read(
            self.OEM_MODEL,
            [["shipped_date", "!=", False], ["received_date", "=", False]],
            fields=_OEM_FIELDS, limit=limit, order="shipped_date asc",
        )

    def log_oem_shipment(
        self, repair_id: int, name: str, carrier: str = "other",
        tracking_ref: Optional[str] = None, shipped_date: Optional[str] = None,
    ) -> dict:
        """Record a part/unit sent out to an OEM or depot.

        Args:
            repair_id: Repair the shipment belongs to.
            name: What went out.
            carrier: One of :data:`OEM_CARRIERS`.
            tracking_ref: Tracking number.
            shipped_date: ``YYYY-MM-DD``; defaults to today.
        """
        if carrier not in OEM_CARRIERS:
            raise ValueError(
                f"carrier must be one of {OEM_CARRIERS}, got {carrier!r}"
            )
        self._require()
        from datetime import date as _date
        values: dict[str, Any] = {
            "order_id": repair_id,
            "name": name,
            "carrier": carrier,
            "shipped_date": shipped_date or _date.today().strftime("%Y-%m-%d"),
        }
        if tracking_ref:
            values["tracking_ref"] = tracking_ref
        ship_id = self.client.create(self.OEM_MODEL, values)
        return {
            "summary": (
                f"OEM shipment '{name}' logged on repair {repair_id} "
                f"({carrier.upper()}{' ' + tracking_ref if tracking_ref else ''})"
            ),
            "shipment_id": ship_id,
            "shipments": self.get_oem_shipments(repair_id),
        }

    def receive_oem_shipment(
        self, shipment_id: int, received_date: Optional[str] = None
    ) -> dict:
        """Mark an OEM shipment as returned."""
        self._require()
        from datetime import date as _date
        self.client.write(self.OEM_MODEL, shipment_id, {
            "received_date": received_date or _date.today().strftime("%Y-%m-%d"),
        })
        rows = self.client.read(self.OEM_MODEL, [shipment_id], fields=_OEM_FIELDS)
        row = rows[0] if rows else {}
        return {
            "summary": f"OEM shipment '{row.get('name')}' marked received",
            "shipment": row,
        }

    # ── Stages and intake batches ────────────────────────────────────

    def stages(self) -> list[dict]:
        """Bench stages in board order, with the native state each maps to.

        Useful before :meth:`move_stage` — ``state`` is computed from
        ``stage_id`` via this map, which is why it can never be written
        directly.
        """
        self._require()
        return self.client.search_read(
            self.STAGE_MODEL, [], fields=_STAGE_FIELDS, order="sequence, id",
        )

    def move_stage(self, repair_id: int, stage: Any) -> dict:
        """Move a repair to a bench stage by id or name.

        This is the supported way to change where a ticket sits: ``state`` is
        readonly and derived from the stage's ``state_map``. Note it moves the
        card without firing the stage's button-method side effects — use the
        allowlisted lifecycle actions when those matter.
        """
        self._require()
        resolved = self._resolve_one(stage, self.STAGE_MODEL, field="name")
        if not resolved:
            candidates = self._resolve_candidates(
                str(stage), self.STAGE_MODEL, field="name"
            )
            raise ValueError(
                f"No repair stage matching {stage!r}."
                + (f" Did you mean: {', '.join(c['name'] for c in candidates)}?"
                   if candidates else "")
            )
        record = self.update(repair_id, {"stage_id": resolved["id"]})
        return {
            "summary": (
                f"{record['name']} moved to stage '{resolved['name']}' "
                f"(state now {record.get('state')})"
            ),
            "repair": record,
        }

    def get_intake_batch(self, batch_id: int) -> dict:
        """A multi-device drop-off, with the tickets it produced."""
        self._require()
        rows = self.client.read(
            self.BATCH_MODEL, [batch_id], fields=_BATCH_FIELDS
        )
        if not rows:
            raise ValueError(f"No repair.intake.batch with id {batch_id}")
        batch = rows[0]
        orders = self.search([["intake_batch_id", "=", batch_id]], limit=100)
        return {
            "summary": (
                f"Intake {batch.get('name')} for "
                f"{_name_of(batch.get('partner_id'))}: {len(orders)} ticket(s)"
            ),
            "batch": batch,
            "repairs": orders,
        }

    def open_intake_batches(self, limit: int = 50) -> list[dict]:
        """Drop-offs where the device is logged but not yet in hand."""
        self._require()
        return self.client.search_read(
            self.BATCH_MODEL, [["device_in_hand", "=", False]],
            fields=_BATCH_FIELDS, limit=limit, order="id desc",
        )


def _name_of(value: Any) -> str:
    """Render a many2one ``[id, name]`` pair as its name."""
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    return str(value) if value else "—"
