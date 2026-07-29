"""
ITAD operations for the ``projects-custom`` module (+ ``product_export_itad``).

ITAD (IT Asset Disposition) jobs live on a model named literally ``tasks`` —
not ``project.task``. This is Martin Nikolov's module and the name is a
historical artefact, but it is what the database exposes, so the constant is
spelled out here rather than guessed at call time.

A ``tasks`` record is a client pickup/processing job moving through
quote → new_pickup → processing → holding → complete, carrying:

* inbound manifest lines (``itad.manifest.line``)
* weighed commodity receipts (``itad.commodity.receipt``)
* estimated commodity entries (``itad.commodity.entry``)
* commodity/service pricing snapshot lines
* audit sessions and issued compliance documents

The module exposes three computed gates — ``itad_can_dispatch``,
``itad_can_price``, ``itad_can_receive`` — which is what the ops surface
keys off, rather than reimplementing the readiness rules here.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "reference_code", "partner_id", "state", "priority",
    "pickup_date", "logistic_type", "est_asset_count", "est_pallet_count",
    "settled_status", "sla_deadline", "sla_days_remaining",
]
_DETAIL_FIELDS = _LIST_FIELDS + [
    "contract_id", "quote_ref", "po_ref", "bol_ref",
    "pickup_address_id", "onsite_contact_id", "alternative_contact_id",
    "package_type", "material_condition", "truck_type_id",
    "time_window_start", "time_window_end", "work_permitted_hours",
    "loading_dock", "elevator_access", "lift_gate_required",
    "forklift_driver_assist", "load_from_ground",
    "equipment_location", "equipment_notes", "building_notes",
    "num_items", "pallet_count", "processed_weight", "remaining_weight",
    "recycling_total_weight", "est_total_weight", "est_scrap_value_total",
    "manifest_line_count", "audit_count", "unpriced_count",
    "commodity_unpriced_count", "pricing_snapshot_count",
    "commodity_charge_total", "commodity_pay_total", "service_total",
    "logistics_price", "task_price", "total_pallet_price",
    "buyback_total", "amount_owed", "settled_date",
    "itad_can_dispatch", "itad_can_price", "itad_can_receive",
    "dest_warehouse_id", "user_ids",
]

_MANIFEST_FIELDS = ["id", "display_name", "task_id"]
_RECEIPT_FIELDS = [
    "id", "task_id", "commodity_id", "audit_session_id",
    "location_id", "employee_id", "create_date",
]
_CONTRACT_FIELDS = [
    "id", "partner_id", "owner_id", "devaluation_template_id",
    "fmv_pricelist_id",
]

#: tasks.state values, in workflow order.
STATES = ["quote", "new_pickup", "processing", "holding", "complete"]

#: How the material moves.
LOGISTIC_TYPES = ["our_trucking", "common_carrier", "client_drop_off"]

#: Packaging on arrival.
PACKAGE_TYPES = ["pallets_48x40", "gaylords", "loose", "other"]

#: Job priority.
PRIORITIES = ["low", "medium", "high"]


def _within_sla(remaining: Any, days: int) -> bool:
    """Whether a job is inside the SLA-risk window.

    ``sla_days_remaining`` is ``False`` on jobs with no SLA deadline, which
    must not be read as "0 days left" — those are excluded.
    """
    if remaining is False or remaining is None:
        return False
    try:
        return float(remaining) <= days
    except (TypeError, ValueError):
        return False


class ITADOps(BaseOps):
    """Read-and-key-writes over ITAD jobs.

    Pricing, devaluation, and commodity-breakdown templates are deliberately
    out of scope — those are configuration a human curates, not something an
    agent should be reaching into.
    """

    MODEL = "tasks"
    MODULE = "projects-custom"
    MANIFEST_MODEL = "itad.manifest.line"
    RECEIPT_MODEL = "itad.commodity.receipt"
    CONTRACT_MODEL = "itad.contract"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "priority desc, pickup_date asc"

    #: No button methods are allowlisted: ITAD state changes carry
    #: compliance weight (certificates of destruction, settlement) and are
    #: driven from the module's own UI. Reads and scoped writes only.
    ALLOWED_ACTIONS = frozenset()

    # ── Reads ────────────────────────────────────────────────────────

    def open_jobs(self, limit: int = 50) -> list[dict]:
        """Jobs not yet complete."""
        return self.search([["state", "!=", "complete"]], limit=limit)

    def jobs_in_state(self, state: str, limit: int = 50) -> list[dict]:
        """Jobs in a given lifecycle state."""
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {state!r}")
        return self.search([["state", "=", state]], limit=limit)

    def jobs_for_customer(self, partner_id: int, limit: int = 50) -> list[dict]:
        """All ITAD jobs for a client."""
        return self.search([["partner_id", "=", partner_id]], limit=limit)

    def upcoming_pickups(self, days: int = 7, limit: int = 50) -> list[dict]:
        """Scheduled pickups in the next *days* days."""
        from datetime import date, timedelta
        today = date.today()
        return self.search(
            [["pickup_date", ">=", today.isoformat()],
             ["pickup_date", "<=", (today + timedelta(days=days)).isoformat()],
             ["state", "!=", "complete"]],
            limit=limit, order="pickup_date asc",
        )

    # The three readiness gates and sla_days_remaining are computed and not
    # stored, so each is filtered client-side over the open-job set rather
    # than in a domain (where Odoo would silently ignore the clause).

    def ready_to_dispatch(self, limit: int = 50) -> list[dict]:
        """Jobs the module considers dispatchable."""
        return self._by_gate("itad_can_dispatch", limit)

    def ready_to_price(self, limit: int = 50) -> list[dict]:
        """Jobs the module considers priceable."""
        return self._by_gate("itad_can_price", limit)

    def ready_to_receive(self, limit: int = 50) -> list[dict]:
        """Jobs the module considers receivable."""
        return self._by_gate("itad_can_receive", limit)

    def _by_gate(self, gate: str, limit: int) -> list[dict]:
        """Filter open jobs on one of the computed readiness gates."""
        return self.search_computed(
            [["state", "!=", "complete"]],
            lambda r, g=gate: bool(r.get(g)),
            limit=limit, extra_fields=[gate],
        )

    def unpriced_jobs(self, limit: int = 50) -> list[dict]:
        """Jobs carrying commodity lines that still have no price."""
        return self.search(
            [["commodity_unpriced_count", ">", 0], ["state", "!=", "complete"]],
            limit=limit, order="commodity_unpriced_count desc",
        )

    def sla_at_risk(self, days: int = 3, limit: int = 50) -> list[dict]:
        """Open jobs within *days* of their SLA deadline.

        ``sla_days_remaining`` is computed and not stored, so it is filtered
        and sorted client-side over the open-job set.
        """
        rows = self.search_computed(
            [["state", "!=", "complete"]],
            lambda r: _within_sla(r.get("sla_days_remaining"), days),
            limit=limit, extra_fields=["sla_days_remaining"],
        )
        return sorted(rows, key=lambda r: r.get("sla_days_remaining") or 0)

    def unsettled_jobs(self, limit: int = 50) -> list[dict]:
        """Completed jobs with money still outstanding."""
        return self.search(
            [["state", "=", "complete"], ["amount_owed", "!=", 0]],
            limit=limit, order="amount_owed desc",
        )

    def get_manifest(self, task_id: int, limit: int = 500) -> list[dict]:
        """Inbound manifest lines for a job."""
        self._require()
        return self.client.search_read(
            self.MANIFEST_MODEL, [["task_id", "=", task_id]],
            fields=_MANIFEST_FIELDS, limit=limit,
        )

    def get_commodity_receipts(self, task_id: int, limit: int = 500) -> list[dict]:
        """Weighed commodity receipts recorded against a job."""
        self._require()
        return self.client.search_read(
            self.RECEIPT_MODEL, [["task_id", "=", task_id]],
            fields=_RECEIPT_FIELDS, limit=limit,
        )

    def get_job_detail(self, task_id: int) -> dict:
        """Full job view: detail fields plus manifest and receipts."""
        record = self.get(task_id)
        record["manifest_lines"] = self.get_manifest(task_id)
        record["commodity_receipts"] = self.get_commodity_receipts(task_id)
        return record

    def contracts(self, partner_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        """ITAD contracts, optionally scoped to one client."""
        self._require()
        domain = [["partner_id", "=", partner_id]] if partner_id else []
        return self.client.search_read(
            self.CONTRACT_MODEL, domain, fields=_CONTRACT_FIELDS, limit=limit
        )

    # ── Scoped writes ────────────────────────────────────────────────

    def schedule_pickup(
        self,
        task_id: int,
        pickup_date: str,
        time_window_start: Optional[str] = None,
        time_window_end: Optional[str] = None,
        truck_type_id: Optional[int] = None,
    ) -> dict:
        """Set the pickup date and optional time window on a job.

        Scheduling is a logistics decision, not a compliance one, so it is
        one of the few writes exposed here.
        """
        values: dict[str, Any] = {"pickup_date": pickup_date}
        if time_window_start:
            values["time_window_start"] = time_window_start
        if time_window_end:
            values["time_window_end"] = time_window_end
        if truck_type_id:
            values["truck_type_id"] = truck_type_id
        record = self.update(task_id, values)
        window = ""
        if time_window_start and time_window_end:
            window = f" ({time_window_start}–{time_window_end})"
        return {
            "summary": f"ITAD job {record.get('reference_code') or task_id} "
                       f"pickup set for {pickup_date}{window}",
            "job": record,
        }

    def set_estimates(
        self,
        task_id: int,
        est_asset_count: Optional[int] = None,
        est_pallet_count: Optional[int] = None,
        est_total_weight: Optional[float] = None,
    ) -> dict:
        """Record pre-pickup volume estimates used for truck sizing."""
        values: dict[str, Any] = {}
        if est_asset_count is not None:
            values["est_asset_count"] = int(est_asset_count)
        if est_pallet_count is not None:
            values["est_pallet_count"] = int(est_pallet_count)
        if est_total_weight is not None:
            values["est_total_weight"] = float(est_total_weight)
        if not values:
            raise ValueError("Provide at least one estimate to set.")
        record = self.update(task_id, values)
        return {
            "summary": f"Estimates updated on ITAD job "
                       f"{record.get('reference_code') or task_id}",
            "job": record,
        }

    # ── Summary ──────────────────────────────────────────────────────

    def ops_summary(self) -> dict:
        """Job counts by state plus the readiness and risk queues."""
        by_state = {s: self.count([["state", "=", s]]) for s in STATES}
        open_jobs = sum(by_state[s] for s in STATES if s != "complete")
        open_domain = [["state", "!=", "complete"]]
        # commodity_unpriced_count IS stored, so this one can filter server-side.
        unpriced = self.count(
            [["commodity_unpriced_count", ">", 0], ["state", "!=", "complete"]]
        )
        at_risk = self.count_computed(
            open_domain,
            lambda r: _within_sla(r.get("sla_days_remaining"), 3),
            extra_fields=["sla_days_remaining"],
        )
        dispatchable = self.count_computed(
            open_domain, lambda r: bool(r.get("itad_can_dispatch")),
            extra_fields=["itad_can_dispatch"],
        )
        return {
            "summary": (
                f"ITAD: {open_jobs} open jobs "
                f"({by_state['new_pickup']} awaiting pickup, "
                f"{by_state['processing']} processing), "
                f"{dispatchable} ready to dispatch, "
                f"{unpriced} with unpriced commodities, "
                f"{at_risk} within 3 days of SLA"
            ),
            "by_state": by_state,
            "open": open_jobs,
            "ready_to_dispatch": dispatchable,
            "unpriced": unpriced,
            "sla_at_risk": at_risk,
        }
