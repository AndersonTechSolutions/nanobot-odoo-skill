"""
Inbound package operations for the ``inbound_tracking`` module
(Odoo 17 ``inbound.shipment``).

The module tracks packages *coming in* — eBay/Amazon purchases, RMA returns,
manual entries — from the moment a tracking number is known until someone
physically confirms the box was opened and its contents accounted for. A
carrier-polling cron advances ``status``; a confirmation deadline turns
unconfirmed-but-delivered packages ``overdue``.

Two things shape this class:

* **Confirmation is a write, not a button.** The module has no
  ``action_confirm``; the UI sets ``status``, ``confirmed_at`` and
  ``confirmed_by`` together. Those three are one audit record, so
  :meth:`InboundOps.confirm_receipt` writes them as a unit rather than
  leaving a caller to set ``status`` alone and produce a confirmed shipment
  with no confirmer.

* **Status is carrier-owned.** ``_poll_carriers`` overwrites ``status`` from
  the carrier API on every poll for anything still ``pending`` /
  ``in_transit`` / ``out_for_delivery``. Hand-writing those states is
  pointless — the next cron run reverts it. Only the terminal states
  (``confirmed``, ``exception``) survive, which is why this class exposes
  confirmation but not a general status setter.

The module also ships ``get_dashboard_data`` as an ``@api.model`` method
backing its TV display. :meth:`InboundOps.dashboard` wraps it so an agent
gets the whole board in one round-trip instead of six counts — see
:meth:`BaseOps._call_model` for why it is called with no ids list.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps, utc_stamp

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "inbound_ref", "tracking_number", "carrier", "status", "source",
    "external_order_id", "delivered_at", "confirm_deadline", "po_id",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "source_document", "source_res_model", "source_res_id",
    "confirmed_at", "confirmed_by", "last_polled_at", "overdue_notified_at",
    "picking_id", "notes", "line_ids", "company_id", "create_date",
]

_LINE_FIELDS = [
    "id", "shipment_id", "product_id", "product_name", "qty_expected",
    "qty_received", "inspected", "disposition", "product_type",
    "serial_numbers", "repair_order_id", "external_item_id",
]

#: ``status`` values.
STATUSES = [
    "pending", "in_transit", "out_for_delivery", "delivered",
    "confirmed", "overdue", "exception",
]

#: Statuses where the package has not yet arrived.
IN_FLIGHT = ["pending", "in_transit", "out_for_delivery"]

#: Statuses needing a human — the action queue the TV dashboard leads with.
ACTION_QUEUE = ["delivered", "overdue", "exception"]

#: ``carrier`` values.
CARRIERS = ["ups", "usps", "fedex", "unknown"]

#: ``source`` values.
SOURCES = ["manual", "ebay", "amazon", "rma"]


class InboundOps(BaseOps):
    """Operations on ``inbound.shipment`` and its lines."""

    MODEL = "inbound.shipment"
    MODULE = "inbound_tracking"
    LINE_MODEL = "inbound.shipment.line"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "confirm_deadline asc, id desc"

    ALLOWED_ACTIONS = frozenset({
        "action_open_source",
        "action_print_label_zpl",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def in_flight(self, limit: int = 50) -> list[dict]:
        """Packages still on the way."""
        return self.search([["status", "in", IN_FLIGHT]], limit=limit)

    def action_queue(self, limit: int = 50) -> list[dict]:
        """Packages needing eyes on them — delivered, overdue, or in exception.

        Ordered by confirmation deadline so the most pressing sits first.
        """
        return self.search(
            [["status", "in", ACTION_QUEUE]], limit=limit,
            order="confirm_deadline asc, id desc",
        )

    def awaiting_confirmation(self, limit: int = 50) -> list[dict]:
        """Delivered packages nobody has confirmed yet."""
        return self.search(
            [["status", "=", "delivered"], ["confirmed_at", "=", False]],
            limit=limit,
        )

    def overdue(self, limit: int = 50) -> list[dict]:
        """Packages past their confirmation deadline.

        ``status`` is flipped to ``overdue`` by the ``_check_deadlines`` cron,
        so this also catches anything delivered whose deadline has passed but
        whose cron run has not landed yet.
        """
        now = utc_stamp()
        return self.search(
            ["|",
             ["status", "=", "overdue"],
             "&", "&",
             ["status", "not in", ["confirmed", "overdue"]],
             ["confirm_deadline", "!=", False],
             ["confirm_deadline", "<", now]],
            limit=limit,
        )

    def exceptions(self, limit: int = 50) -> list[dict]:
        """Packages the carrier reported a problem with."""
        return self.search([["status", "=", "exception"]], limit=limit)

    def find_by_tracking(self, tracking_number: str, limit: int = 10) -> list[dict]:
        """Locate a shipment by tracking number or internal label id."""
        return self.search(
            ["|",
             ["tracking_number", "ilike", tracking_number],
             ["inbound_ref", "ilike", tracking_number]],
            limit=limit,
        )

    def find_by_order(self, order_ref: str, limit: int = 10) -> list[dict]:
        """Locate shipments by the marketplace order they came from."""
        return self.search(
            ["|",
             ["external_order_id", "ilike", order_ref],
             ["source_document", "ilike", order_ref]],
            limit=limit,
        )

    def shipments_for_po(self, po_id: int, limit: int = 50) -> list[dict]:
        """Inbound packages linked to a purchase order."""
        return self.search([["po_id", "=", po_id]], limit=limit)

    def from_source(self, source: str, limit: int = 50) -> list[dict]:
        """Packages from one origin — ``manual``, ``ebay``, ``amazon`` or ``rma``."""
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
        return self.search([["source", "=", source]], limit=limit)

    def stale_polls(self, limit: int = 50) -> list[dict]:
        """In-flight packages the carrier poller has never reached.

        A never-polled shipment means the carrier client is unconfigured or
        erroring for that company — the cron marks ``last_polled_at`` even on
        failure specifically so this stays diagnosable.
        """
        return self.search(
            [["status", "in", IN_FLIGHT], ["last_polled_at", "=", False]],
            limit=limit, order="create_date asc",
        )

    # ── Lines ────────────────────────────────────────────────────────

    def get_lines(self, shipment_id: int) -> list[dict]:
        """Expected contents of a shipment."""
        self._require()
        return self.client.search_read(
            self.LINE_MODEL, [["shipment_id", "=", shipment_id]],
            fields=_LINE_FIELDS, order="id",
        )

    def uninspected_lines(self, limit: int = 50) -> list[dict]:
        """Contents of delivered packages that nobody has inspected yet."""
        self._require()
        return self.client.search_read(
            self.LINE_MODEL,
            [["inspected", "=", False],
             ["shipment_id.status", "in", ["delivered", "confirmed", "overdue"]]],
            fields=_LINE_FIELDS, limit=limit, order="shipment_id, id",
        )

    def lines_for_repair(self, repair_order_id: int) -> list[dict]:
        """Inbound parts earmarked for a repair order."""
        self._require()
        return self.client.search_read(
            self.LINE_MODEL, [["repair_order_id", "=", repair_order_id]],
            fields=_LINE_FIELDS, order="id",
        )

    # ── Writes ───────────────────────────────────────────────────────

    def create_shipment(
        self,
        tracking_number: str,
        carrier: str = "unknown",
        source: str = "manual",
        external_order_id: Optional[str] = None,
        po_id: Optional[int] = None,
        notes: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Register an inbound package.

        Args:
            tracking_number: Carrier tracking number. Required by the model.
            carrier: One of :data:`CARRIERS`. ``unknown`` is legal — the
                poller simply skips it until a carrier is set.
            source: One of :data:`SOURCES`.
            external_order_id: Marketplace order reference, when there is one.
            po_id: Linked ``purchase.order``.
            notes: Free text.
            **extra: Any other ``inbound.shipment`` field.

        Returns:
            The created shipment in detail form, starting at ``pending``.
        """
        if carrier not in CARRIERS:
            raise ValueError(f"carrier must be one of {CARRIERS}, got {carrier!r}")
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {source!r}")

        values: dict[str, Any] = {
            "tracking_number": tracking_number,
            "carrier": carrier,
            "source": source,
            "status": "pending",
        }
        if external_order_id:
            values["external_order_id"] = external_order_id
        if po_id:
            values["po_id"] = po_id
        if notes:
            values["notes"] = notes
        values.update(extra)

        record = self.create(values)
        return {
            "summary": (
                f"Inbound {record.get('inbound_ref') or record['id']} registered "
                f"({carrier.upper()} {tracking_number}, source {source})"
            ),
            "shipment": record,
        }

    def add_line(
        self,
        shipment_id: int,
        product_id: Optional[int] = None,
        product_name: Optional[str] = None,
        qty_expected: float = 1.0,
        disposition: str = "inventory",
        repair_order_id: Optional[int] = None,
        **extra: Any,
    ) -> dict:
        """Add an expected item to a shipment.

        Either *product_id* or *product_name* should be given — the module
        keeps ``product_name`` for items that have no Odoo product yet, which
        is common for marketplace purchases received before cataloguing.
        """
        if not product_id and not product_name:
            raise ValueError("Provide product_id or product_name")
        if disposition not in ("inventory", "non_inventory"):
            raise ValueError(
                f"disposition must be 'inventory' or 'non_inventory', "
                f"got {disposition!r}"
            )
        self._require()
        values: dict[str, Any] = {
            "shipment_id": shipment_id,
            "qty_expected": qty_expected,
            "disposition": disposition,
        }
        if product_id:
            values["product_id"] = product_id
        if product_name:
            values["product_name"] = product_name
        if repair_order_id:
            values["repair_order_id"] = repair_order_id
        values.update(extra)
        line_id = self.client.create(self.LINE_MODEL, values)
        return {
            "summary": f"Line #{line_id} added to shipment {shipment_id}",
            "line_id": line_id,
            "lines": self.get_lines(shipment_id),
        }

    def receive_line(
        self,
        line_id: int,
        qty_received: float,
        serial_numbers: Optional[str] = None,
        inspected: bool = True,
    ) -> dict:
        """Record what actually turned up for one line.

        Args:
            line_id: ``inbound.shipment.line`` to update.
            qty_received: Quantity actually in the box.
            serial_numbers: Newline- or comma-separated serials, if captured.
            inspected: Mark the line inspected (default). Pass ``False`` to
                record a count without claiming it was checked over.
        """
        self._require()
        values: dict[str, Any] = {
            "qty_received": qty_received,
            "inspected": inspected,
        }
        if serial_numbers:
            values["serial_numbers"] = serial_numbers
        self.client.write(self.LINE_MODEL, line_id, values)
        rows = self.client.read(self.LINE_MODEL, [line_id], fields=_LINE_FIELDS)
        line = rows[0] if rows else {}
        expected = line.get("qty_expected") or 0
        short = expected - (line.get("qty_received") or 0)
        return {
            "summary": (
                f"Line {line_id}: received {qty_received} of {expected}"
                + (f" — {short} short" if short > 0 else "")
            ),
            "short_by": short if short > 0 else 0,
            "line": line,
        }

    def confirm_receipt(self, shipment_id: int, notes: Optional[str] = None) -> dict:
        """Confirm a package was opened and its contents accounted for.

        The module has no confirm button — the UI writes ``status``,
        ``confirmed_at`` and ``confirmed_by`` together, and they only make
        sense together: a ``confirmed`` shipment with no confirmer is an
        audit hole. So all three go in one write, stamped with the API user.

        Reports any line still short or uninspected rather than silently
        confirming over an incomplete receipt — confirming is still allowed
        (a short shipment is a real outcome), but the caller is told.
        """
        self._require()
        lines = self.get_lines(shipment_id)
        short = [
            line for line in lines
            if (line.get("qty_expected") or 0) > (line.get("qty_received") or 0)
        ]
        unchecked = [line for line in lines if not line.get("inspected")]

        values: dict[str, Any] = {
            "status": "confirmed",
            "confirmed_at": utc_stamp(),
            "confirmed_by": self.client.uid,
        }
        if notes:
            values["notes"] = notes
        record = self.update(shipment_id, values)

        warnings = []
        if short:
            warnings.append(f"{len(short)} line(s) short")
        if unchecked:
            warnings.append(f"{len(unchecked)} line(s) not marked inspected")
        return {
            "summary": (
                f"Shipment {record.get('inbound_ref') or shipment_id} confirmed"
                + (" — " + ", ".join(warnings) if warnings else "")
            ),
            "warnings": warnings,
            "short_lines": short,
            "uninspected_lines": unchecked,
            "shipment": record,
        }

    def flag_exception(self, shipment_id: int, reason: str) -> dict:
        """Mark a shipment as a problem (damaged, lost, wrong contents).

        ``exception`` is one of the two states the carrier poller will not
        overwrite, so this sticks until someone resolves it.
        """
        record = self.update(
            shipment_id, {"status": "exception", "notes": reason}
        )
        return {
            "summary": (
                f"Shipment {record.get('inbound_ref') or shipment_id} flagged as "
                f"an exception: {reason}"
            ),
            "shipment": record,
        }

    # ── Dashboard / summary ──────────────────────────────────────────

    def dashboard(self) -> Any:
        """Fetch the module's own TV-dashboard aggregate in one round-trip.

        ``get_dashboard_data`` is an ``@api.model`` method, so it takes no ids
        list — see :meth:`BaseOps._call_model`. Preferred over assembling the
        same picture from six ``search_count`` calls, and it stays in step
        with the module's own bucket definitions.
        """
        return self._call_model("get_dashboard_data")

    def inbound_summary(self) -> dict:
        """Counts by status plus the two queues that need a human."""
        counts = {s: self.count([["status", "=", s]]) for s in STATUSES}
        in_flight = sum(counts[s] for s in IN_FLIGHT)
        queue = sum(counts[s] for s in ACTION_QUEUE)
        unconfirmed = self.count(
            [["status", "=", "delivered"], ["confirmed_at", "=", False]]
        )
        return {
            "summary": (
                f"Inbound: {in_flight} in flight, {queue} need attention "
                f"({unconfirmed} delivered unconfirmed, {counts['overdue']} overdue, "
                f"{counts['exception']} exceptions)"
            ),
            "by_status": counts,
            "in_flight": in_flight,
            "action_queue": queue,
            "awaiting_confirmation": unconfirmed,
        }
