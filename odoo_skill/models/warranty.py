"""
Warranty operations for the ``atech_warranty`` module.

Three related models:

* ``warranty.registration`` — a serial (``stock.lot``) covered for N months
  from a start date. The unit of coverage.
* ``warranty.claim`` — a customer reporting a fault against a registration;
  can escalate into an RMA.
* ``warranty.extension`` — additional months sold onto a registration.

Coverage is derived from ``start_date`` + ``months``; ``end_date`` and
``in_warranty`` are computed, so never write them.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_REG_LIST_FIELDS = [
    "id", "name", "partner_id", "product_id", "lot_id",
    "start_date", "end_date", "months", "state", "source",
]
_REG_DETAIL_FIELDS = _REG_LIST_FIELDS + [
    "claim_count", "claim_ids", "extension_ids", "note",
    "sale_order_id", "purchase_order_id", "picking_id",
    "confirmation_sent", "expiry_warning_sent",
]

_CLAIM_LIST_FIELDS = [
    "id", "name", "registration_id", "partner_id", "product_id",
    "lot_id", "date", "state", "in_warranty",
]
_CLAIM_DETAIL_FIELDS = _CLAIM_LIST_FIELDS + ["description", "rma_id", "fsm_task_count"]

_EXT_FIELDS = ["id", "registration_id", "months", "price", "sale_order_id"]

#: warranty.registration.state values.
REGISTRATION_STATES = ["active", "expired", "cancelled"]
#: warranty.claim.state values.
CLAIM_STATES = ["new", "rma", "closed"]
#: How a registration came to exist.
SOURCES = ["sale", "purchase", "manual"]


class WarrantyOps(BaseOps):
    """Operations across registrations, claims, and extensions."""

    MODEL = "warranty.registration"
    MODULE = "atech_warranty"
    CLAIM_MODEL = "warranty.claim"
    EXTENSION_MODEL = "warranty.extension"
    LIST_FIELDS = _REG_LIST_FIELDS
    DETAIL_FIELDS = _REG_DETAIL_FIELDS
    ORDER = "end_date asc"

    ALLOWED_ACTIONS = frozenset({
        "action_cancel",
        "action_reactivate",
        "action_sell_extension",
    })

    #: Methods permitted on ``warranty.claim`` via :meth:`run_claim_action`.
    ALLOWED_CLAIM_ACTIONS = frozenset({
        "action_close",
        "action_create_rma",
        "action_schedule_fsm_job",
    })

    # ── Registrations ────────────────────────────────────────────────

    def active_registrations(self, limit: int = 50) -> list[dict]:
        """Registrations currently in force."""
        return self.search([["state", "=", "active"]], limit=limit)

    def expiring_soon(self, days: int = 30, limit: int = 50) -> list[dict]:
        """Active registrations expiring within *days*."""
        return self.search(self._expiring_domain(days), limit=limit)

    def _expiring_domain(self, days: int = 30) -> list:
        """Domain for the expiry queue — shared by the list and the count.

        Fully server-side, so :meth:`warranty_summary` can count it exactly
        instead of reporting the length of a capped page.
        """
        from datetime import date, timedelta
        cutoff = (date.today() + timedelta(days=days)).isoformat()
        return [
            ["state", "=", "active"],
            ["end_date", "<=", cutoff],
            ["end_date", ">=", date.today().isoformat()],
        ]

    def find_by_serial(self, serial: str, limit: int = 10) -> list[dict]:
        """Find registrations covering a serial number."""
        return self.search([["lot_id.name", "ilike", serial]], limit=limit)

    def registrations_for_customer(self, partner_id: int, limit: int = 50) -> list[dict]:
        """All registrations belonging to a customer."""
        return self.search([["partner_id", "=", partner_id]], limit=limit)

    def check_coverage(self, serial: str) -> dict:
        """Answer 'is this serial under warranty?' for a chat caller.

        Returns a structured verdict rather than raising, because "no
        registration exists" is a normal answer, not an error.
        """
        matches = self.find_by_serial(serial, limit=5)
        if not matches:
            return {
                "summary": f"No warranty registration found for serial {serial}.",
                "covered": False,
                "found": False,
                "registrations": [],
            }
        active = [m for m in matches if m.get("state") == "active"]
        best = active[0] if active else matches[0]
        covered = bool(active)
        return {
            "summary": (
                f"Serial {serial}: {'COVERED' if covered else 'NOT covered'} "
                f"— {best.get('name')} "
                f"({best.get('state')}, ends {best.get('end_date') or '—'})"
            ),
            "covered": covered,
            "found": True,
            "registration": best,
            "registrations": matches,
        }

    def create_registration(
        self,
        partner_id: int,
        lot_id: int,
        months: int,
        start_date: str,
        source: str = "manual",
        **extra: Any,
    ) -> dict:
        """Register a serial for warranty coverage.

        Args:
            partner_id: Covered customer.
            lot_id: ``stock.lot`` id (the serial).
            months: Coverage length in months.
            start_date: ``YYYY-MM-DD`` coverage start.
            source: One of :data:`SOURCES`.
        """
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
        values: dict[str, Any] = {
            "partner_id": partner_id,
            "lot_id": lot_id,
            "months": int(months),
            "start_date": start_date,
            "source": source,
        }
        values.update(extra)
        record = self.create(values)
        return {
            "summary": (
                f"Registration {record['name']} created — {months} months "
                f"from {start_date} (ends {record.get('end_date') or '?'})"
            ),
            "registration": record,
        }

    # ── Claims ───────────────────────────────────────────────────────

    def open_claims(self, limit: int = 50) -> list[dict]:
        """Claims not yet closed."""
        self._require()
        return self.client.search_read(
            self.CLAIM_MODEL, [["state", "!=", "closed"]],
            fields=_CLAIM_LIST_FIELDS, limit=limit, order="date desc",
        )

    def get_claim(self, claim_id: int) -> dict:
        """Read one claim in detail."""
        self._require()
        rows = self.client.read(
            self.CLAIM_MODEL, [claim_id], fields=_CLAIM_DETAIL_FIELDS
        )
        if not rows:
            from ..errors import OdooRecordNotFoundError
            raise OdooRecordNotFoundError(f"No warranty.claim with id {claim_id}")
        return rows[0]

    def claims_for_registration(self, registration_id: int) -> list[dict]:
        """All claims filed against a registration."""
        self._require()
        return self.client.search_read(
            self.CLAIM_MODEL, [["registration_id", "=", registration_id]],
            fields=_CLAIM_LIST_FIELDS, limit=100,
        )

    def create_claim(
        self,
        registration_id: int,
        description: str,
        date: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """File a warranty claim against a registration.

        ``date`` and ``description`` are both required by the model; *date*
        defaults to today.
        """
        self._require()
        if not date:
            from datetime import date as _date
            date = _date.today().isoformat()
        values: dict[str, Any] = {
            "registration_id": registration_id,
            "description": description,
            "date": date,
        }
        values.update(extra)
        claim_id = self.client.create(self.CLAIM_MODEL, values)
        record = self.get_claim(claim_id)
        return {
            "summary": f"Warranty claim {record.get('name')} filed "
                       f"(in warranty: {record.get('in_warranty')})",
            "claim": record,
        }

    def run_claim_action(self, claim_id: int, method: str, **kwargs: Any) -> dict:
        """Invoke an allowlisted button method on a ``warranty.claim``."""
        self._require()
        if method not in self.ALLOWED_CLAIM_ACTIONS:
            from ._base import OdooActionNotAllowedError
            raise OdooActionNotAllowedError(
                f"Method '{method}' is not permitted on {self.CLAIM_MODEL}. "
                f"Allowed: {', '.join(sorted(self.ALLOWED_CLAIM_ACTIONS))}"
            )
        raw = self.client.execute(self.CLAIM_MODEL, method, [claim_id], **kwargs)
        return {
            "model": self.CLAIM_MODEL,
            "id": claim_id,
            "method": method,
            "returned": raw if not isinstance(raw, dict) else {
                "res_model": raw.get("res_model"), "res_id": raw.get("res_id"),
            },
            "record": self.get_claim(claim_id),
        }

    # ── Extensions ───────────────────────────────────────────────────

    def extensions_for(self, registration_id: int) -> list[dict]:
        """Extensions sold onto a registration."""
        self._require()
        return self.client.search_read(
            self.EXTENSION_MODEL, [["registration_id", "=", registration_id]],
            fields=_EXT_FIELDS, limit=50,
        )

    # ── Summary ──────────────────────────────────────────────────────

    def warranty_summary(self) -> dict:
        """Registration and claim counts — a desk overview."""
        reg = {s: self.count([["state", "=", s]]) for s in REGISTRATION_STATES}
        self._require()
        claims = {
            s: self.client.search_count(self.CLAIM_MODEL, [["state", "=", s]])
            for s in CLAIM_STATES
        }
        expiring = self.count(self._expiring_domain(30))
        return {
            "summary": (
                f"Warranty: {reg['active']} active registrations "
                f"({expiring} expiring in 30d), "
                f"{claims['new']} new claims, {claims['rma']} escalated to RMA"
            ),
            "registrations_by_state": reg,
            "claims_by_state": claims,
            "expiring_30d": expiring,
        }
