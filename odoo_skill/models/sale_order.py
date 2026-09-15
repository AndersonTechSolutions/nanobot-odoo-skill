"""
Sales order operations for Odoo ``sale.order`` and ``sale.order.line``.
"""

import logging
from typing import Any, Optional

from ..client import OdooClient

logger = logging.getLogger("odoo_skill")

_ORDER_LIST_FIELDS = [
    "id", "name", "partner_id", "state", "date_order",
    "amount_untaxed", "amount_tax", "amount_total",
    "user_id", "team_id",
]

_ORDER_DETAIL_FIELDS = _ORDER_LIST_FIELDS + [
    "order_line", "invoice_ids", "note",
    "payment_term_id", "pricelist_id",
    "currency_id", "company_id",
]

_LINE_FIELDS = [
    "id", "product_id", "name", "product_uom_qty",
    "price_unit", "discount", "price_subtotal", "tax_id",
]


class SaleOrderOps:
    """High-level operations for Odoo sales orders (``sale.order``).

    Args:
        client: An authenticated :class:`OdooClient` instance.
    """

    MODEL = "sale.order"
    LINE_MODEL = "sale.order.line"
    PICKING_MODEL = "stock.picking"
    MOVE_MODEL = "stock.move"

    def __init__(self, client: OdooClient) -> None:
        self.client = client

    # ── Create ───────────────────────────────────────────────────────

    def create_quotation(
        self,
        partner_id: int,
        lines: list[dict],
        notes: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Create a new sales quotation.

        Args:
            partner_id: Customer ID.
            lines: List of line dicts, each containing at minimum
                ``product_id`` and optionally ``quantity``, ``price_unit``,
                ``discount``.
            notes: Optional order notes.
            **extra: Additional ``sale.order`` field values.

        Returns:
            The newly created order record.

        Example::

            order = sales.create_quotation(
                partner_id=42,
                lines=[
                    {"product_id": 7, "quantity": 10, "price_unit": 49.99},
                    {"product_id": 8, "quantity": 5},
                ],
            )
        """
        order_lines = []
        for line in lines:
            ol: dict[str, Any] = {
                "product_id": line["product_id"],
                "product_uom_qty": line.get("quantity", 1),
            }
            if "price_unit" in line:
                ol["price_unit"] = line["price_unit"]
            if "discount" in line:
                ol["discount"] = line["discount"]
            if "name" in line:
                ol["name"] = line["name"]
            # (0, 0, vals) = create new linked record
            order_lines.append((0, 0, ol))

        values: dict[str, Any] = {
            "partner_id": partner_id,
            "order_line": order_lines,
        }
        if notes:
            values["note"] = notes
        values.update(extra)

        order_id = self.client.create(self.MODEL, values)
        logger.info("Created quotation for partner_id=%d → id=%d", partner_id, order_id)
        return self.get_order(order_id)

    # ── Workflow actions ─────────────────────────────────────────────

    def confirm_order(self, order_id: int) -> dict:
        """Confirm a draft quotation → sales order.

        Args:
            order_id: The sale order ID.

        Returns:
            The updated order record (state should be ``sale``).
        """
        self.client.execute(self.MODEL, "action_confirm", [order_id])
        logger.info("Confirmed order id=%d", order_id)
        return self.get_order(order_id)

    def cancel_order(self, order_id: int) -> dict:
        """Cancel a sales order.

        Args:
            order_id: The sale order ID.

        Returns:
            The updated order record (state should be ``cancel``).
        """
        self.client.execute(self.MODEL, "action_cancel", [order_id])
        logger.info("Cancelled order id=%d", order_id)
        return self.get_order(order_id)

    # ── Delivery ─────────────────────────────────────────────────────

    #: Picking states that still need action to complete a delivery.
    _OPEN_PICK_STATES = ("draft", "waiting", "confirmed", "assigned")

    def get_delivery_status(self, order_id: int) -> dict:
        """Report where an order stands on delivery — read-only.

        Summarises the order's own ``delivery_status`` / ``invoice_status``
        and every outgoing picking in its route (the Pick → Pack → Out chain
        this warehouse uses), so the agent can tell the user what is done and
        what is still open *before* calling :meth:`deliver_order`.

        Also surfaces any linked Field Service / proof-of-delivery task IDs for
        reference. It never completes them — recording the physical delivery is
        :meth:`deliver_order`'s job (the goods pickings); the FSM job is managed
        separately.

        Args:
            order_id: The sale order ID.

        Returns:
            ``{order, state, delivery_status, invoice_status, pickings:[...],
            fsm_task_ids, ready_to_deliver}``. ``delivery_status`` is Odoo's:
            ``pending`` (nothing out yet), ``partial``, or ``full``.
        """
        order = self.client.read(
            self.MODEL, order_id,
            fields=["name", "state", "delivery_status", "invoice_status",
                    "picking_ids", "task_id", "fsm_task_ids"],
        )
        if not order:
            raise ValueError(f"Sale order {order_id} not found.")
        order = order[0]

        pickings = []
        if order.get("picking_ids"):
            pickings = self.client.read(
                self.PICKING_MODEL, order["picking_ids"],
                fields=["name", "state", "picking_type_code", "scheduled_date"],
            )
        ready = any(
            p["state"] == "assigned" and p.get("picking_type_code") == "outgoing"
            for p in pickings
        ) or any(p["state"] in self._OPEN_PICK_STATES for p in pickings)

        fsm = order.get("fsm_task_ids") or []
        if order.get("task_id"):
            tid = order["task_id"][0] if isinstance(order["task_id"], list) else order["task_id"]
            if tid not in fsm:
                fsm = fsm + [tid]

        return {
            "order": order["name"],
            "state": order["state"],
            "delivery_status": order.get("delivery_status"),
            "invoice_status": order.get("invoice_status"),
            "pickings": [
                {"name": p["name"], "state": p["state"],
                 "type": p.get("picking_type_code"),
                 "scheduled": p.get("scheduled_date")}
                for p in pickings
            ],
            "fsm_task_ids": fsm,
            "ready_to_deliver": ready,
        }

    def deliver_order(self, order_id: int, backorder: bool = False) -> dict:
        """Mark a confirmed order's goods as delivered.

        Walks the outgoing picking chain (Pick → Pack → Out) and validates
        each transfer as it becomes ready, so a multi-step warehouse route is
        carried all the way to a delivered Out. For each transfer it sets the
        done quantity to the reserved demand and validates natively
        (``button_validate``); when the next link is only reserved-pending it
        reserves it (``action_assign``) and continues.

        This is the physical-goods delivery only. It flips the order's
        ``delivery_status`` (and thus ``invoice_status`` for delivery-based
        invoicing) but does **not** touch any Field Service / proof-of-delivery
        task — those are managed separately.

        Safety:
          * The order must be a confirmed ``sale`` (or ``done``); a draft
            quotation is refused — confirm it first.
          * Only fully-reserved (``assigned``) transfers are validated. A
            transfer that cannot reserve its stock is reported as blocked and
            left untouched, never force-validated.
          * ``button_validate`` returning a wizard (e.g. a backorder prompt)
            stops that transfer and reports it rather than clicking through a
            dialog blindly. With ``backorder=False`` (default) quantities are
            set to full demand, so a backorder prompt should not arise.

        Args:
            order_id: The sale order ID.
            backorder: Reserved for future partial-delivery handling; when
                False (default) each transfer is delivered in full.

        Returns:
            ``{order, delivery_status, invoice_status, delivered:[names],
            blocked:[{picking, reason}], pickings:[...]}`` — the post-run
            delivery status plus a per-transfer outcome.
        """
        order = self.client.read(
            self.MODEL, order_id, fields=["name", "state", "picking_ids"],
        )
        if not order:
            raise ValueError(f"Sale order {order_id} not found.")
        order = order[0]
        if order["state"] not in ("sale", "done"):
            raise ValueError(
                f"Order {order['name']} is {order['state']!r}, not a confirmed "
                f"sale. Confirm the order before delivering."
            )
        picking_ids = order.get("picking_ids") or []
        if not picking_ids:
            raise ValueError(
                f"Order {order['name']} has no delivery transfers to validate."
            )

        delivered: list[str] = []
        blocked: list[dict] = []
        seen_blocked: set[int] = set()

        # The chain is at most a few links; the cap guards against any loop
        # where nothing makes progress (e.g. stock that never reserves).
        for _ in range(12):
            pickings = self.client.read(
                self.PICKING_MODEL, picking_ids,
                fields=["id", "name", "state", "picking_type_code"],
            )
            open_p = [p for p in pickings if p["state"] in self._OPEN_PICK_STATES]
            if not open_p:
                break

            ready = [p for p in open_p if p["state"] == "assigned"]
            if not ready:
                # Nothing reserved yet — try to reserve the pending links, then
                # re-read. If none can be reserved, report and stop.
                to_assign = [p for p in open_p if p["state"] in ("confirmed", "waiting")]
                for p in to_assign:
                    try:
                        self.client.execute(self.PICKING_MODEL, "action_assign", [p["id"]])
                    except Exception as exc:  # noqa: BLE001 - surfaced, not raised
                        logger.warning("action_assign failed for %s: %s", p["name"], exc)
                # Trust the resulting state, not the call: reservation can no-op
                # silently when the stock isn't there.
                recheck = self.client.read(
                    self.PICKING_MODEL, [p["id"] for p in open_p], fields=["state"],
                )
                if not any(r["state"] == "assigned" for r in recheck):
                    for p in open_p:
                        if p["id"] not in seen_blocked:
                            blocked.append({"picking": p["name"],
                                            "reason": f"cannot reserve stock (state {p['state']})"})
                            seen_blocked.add(p["id"])
                    break
                continue

            progressed = False
            for pick in ready:
                outcome = self._validate_one_picking(pick["id"])
                if outcome["validated"]:
                    delivered.append(pick["name"])
                    progressed = True
                elif pick["id"] not in seen_blocked:
                    blocked.append({"picking": pick["name"], "reason": outcome["reason"]})
                    seen_blocked.add(pick["id"])
            if not progressed:
                break

        final = self.client.read(
            self.MODEL, order_id,
            fields=["name", "delivery_status", "invoice_status", "picking_ids"],
        )[0]
        pickings = self.client.read(
            self.PICKING_MODEL, final["picking_ids"],
            fields=["name", "state", "picking_type_code"],
        )
        logger.info("deliver_order %s → delivery_status=%s (delivered %d, blocked %d)",
                    final["name"], final.get("delivery_status"), len(delivered), len(blocked))
        return {
            "order": final["name"],
            "delivery_status": final.get("delivery_status"),
            "invoice_status": final.get("invoice_status"),
            "delivered": delivered,
            "blocked": blocked,
            "pickings": [
                {"name": p["name"], "state": p["state"], "type": p.get("picking_type_code")}
                for p in pickings
            ],
        }

    def _validate_one_picking(self, picking_id: int) -> dict:
        """Set done quantities and validate one reserved transfer.

        Mirrors what a completed transfer looks like on this Odoo 17 DB: each
        move gets ``quantity`` = its demand and ``picked`` = True, then the
        native ``button_validate`` runs. A wizard return (a dialog Odoo wants a
        human to answer, such as a backorder prompt) is reported, not clicked.

        Returns:
            ``{"validated": bool, "state": <state>, "reason": <str>}``.
        """
        pick = self.client.read(
            self.PICKING_MODEL, picking_id, fields=["name", "state", "move_ids"],
        )
        if not pick:
            return {"validated": False, "reason": "picking vanished"}
        pick = pick[0]
        if pick["state"] != "assigned":
            return {"validated": False, "reason": f"not reserved (state {pick['state']})"}

        for move_id in pick.get("move_ids", []):
            move = self.client.read(
                self.MOVE_MODEL, move_id, fields=["product_uom_qty"],
            )
            if not move:
                continue
            self.client.write(
                self.MOVE_MODEL, move_id,
                {"quantity": move[0]["product_uom_qty"], "picked": True},
            )

        result = self.client.execute(self.PICKING_MODEL, "button_validate", [picking_id])
        if isinstance(result, dict):
            # An act_window / wizard — needs a human answer. Leave it open.
            return {"validated": False,
                    "reason": f"needs manual step: {result.get('res_model') or 'wizard'}"}

        state = self.client.read(
            self.PICKING_MODEL, picking_id, fields=["state"],
        )[0]["state"]
        if state == "done":
            logger.info("Validated delivery transfer %s (id=%d)", pick["name"], picking_id)
            return {"validated": True, "state": state}
        return {"validated": False, "state": state,
                "reason": f"validate did not complete (state {state})"}

    # ── Read ─────────────────────────────────────────────────────────

    def get_order(self, order_id: int) -> dict:
        """Get full details of a single sales order.

        Args:
            order_id: The sale order ID.

        Returns:
            Order record dict.
        """
        records = self.client.read(self.MODEL, order_id, fields=_ORDER_DETAIL_FIELDS)
        return records[0] if records else {}

    def search_orders(
        self,
        partner_id: Optional[int] = None,
        state: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """Search sales orders with optional filters.

        Args:
            partner_id: Filter by customer.
            state: Filter by state (``draft``, ``sent``, ``sale``, ``done``, ``cancel``).
            limit: Max results.
            offset: Pagination offset.

        Returns:
            List of order records.
        """
        domain: list = []
        if partner_id:
            domain.append(["partner_id", "=", partner_id])
        if state:
            domain.append(["state", "=", state])

        return self.client.search_read(
            self.MODEL, domain, fields=_ORDER_LIST_FIELDS,
            limit=limit, offset=offset, order="date_order desc",
        )

    def get_order_lines(self, order_id: int) -> list[dict]:
        """Get the line items for a specific order.

        Args:
            order_id: The sale order ID.

        Returns:
            List of order line dicts.
        """
        return self.client.search_read(
            self.LINE_MODEL,
            [["order_id", "=", order_id]],
            fields=_LINE_FIELDS,
        )
