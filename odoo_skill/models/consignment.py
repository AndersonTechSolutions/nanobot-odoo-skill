"""
Consignment operations for the ``atech_consignment`` module.

An intake ``consignment.order`` groups items taken in from one consignor;
each ``consignment.item`` then runs a long lifecycle from intake through
inspection, pricing, listing, sale, and payout.

The item state machine is the longest in the custom estate (15 states), so
:data:`ITEM_STATES` is grouped into phases by :meth:`ConsignmentOps.pipeline_summary`
rather than reported raw.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_ORDER_LIST_FIELDS = [
    "id", "reference", "consignor_id", "deal_type", "state",
    "intake_date", "item_count", "payout_method", "intake_user_id",
]
_ORDER_DETAIL_FIELDS = _ORDER_LIST_FIELDS + ["item_ids", "customer_signature"]

_ITEM_LIST_FIELDS = [
    "id", "reference", "order_id", "consignor_id", "brand_model",
    "state", "condition_grade", "list_price", "sale_price",
    "estimated_value", "serial_no", "product_id",
]
_ITEM_DETAIL_FIELDS = _ITEM_LIST_FIELDS + [
    "specs", "intake_note", "min_price", "agreed_buy_price",
    "split_pct", "selling_fee_pct", "actual_fees", "net_proceeds",
    "payout_amount", "payout_method", "payment_cleared", "fees_recorded",
    "inspection_passed", "hold_until", "deal_type", "sale_order_id",
    "channel_ids", "photo_ids", "store_credit_id",
]

#: consignment.order.state values.
ORDER_STATES = ["draft", "confirmed", "closed"]

#: consignment.item.state values, in rough lifecycle order.
ITEM_STATES = [
    "draft", "intake", "inspection", "priced", "approved", "listed",
    "sold", "payout_pending", "ready_to_pay", "paid_out", "closed",
    "rejected", "returned", "withdrawn", "refunded",
]

#: Lifecycle phases, for readable summaries.
ITEM_PHASES = {
    "incoming": ["draft", "intake", "inspection"],
    "ready_to_sell": ["priced", "approved"],
    "on_market": ["listed"],
    "settling": ["sold", "payout_pending", "ready_to_pay"],
    "finished": ["paid_out", "closed"],
    "exited": ["rejected", "returned", "withdrawn", "refunded"],
}

#: Deal shapes.
DEAL_TYPES = ["consignment", "outright"]
#: How the consignor gets paid.
PAYOUT_METHODS = ["cash", "check", "store_credit"]
#: Condition grades.
GRADES = ["a", "b", "c", "d"]


class ConsignmentOps(BaseOps):
    """Operations on consignment intake orders and their items."""

    MODEL = "consignment.order"
    MODULE = "atech_consignment"
    ITEM_MODEL = "consignment.item"
    LIST_FIELDS = _ORDER_LIST_FIELDS
    DETAIL_FIELDS = _ORDER_DETAIL_FIELDS
    ORDER = "intake_date desc"

    ALLOWED_ACTIONS = frozenset({
        "action_send_received",
        "action_print_item_labels",
    })

    #: Methods permitted on ``consignment.item`` via :meth:`run_item_action`.
    ALLOWED_ITEM_ACTIONS = frozenset({
        "action_start_inspection",
        "action_set_priced",
        "action_approve",
        "action_list_ebay",
        "action_list_local",
        "action_list_woo",
        "action_release_payout",
        "action_mark_payment_cleared",
        "action_post_payout",
        "action_pull_ebay_fees",
        "action_close",
    })

    # ── Orders ───────────────────────────────────────────────────────

    def open_orders(self, limit: int = 50) -> list[dict]:
        """Intake orders not yet closed."""
        return self.search([["state", "!=", "closed"]], limit=limit)

    def orders_for_consignor(self, partner_id: int, limit: int = 50) -> list[dict]:
        """All intake orders from one consignor."""
        return self.search([["consignor_id", "=", partner_id]], limit=limit)

    def create_order(
        self,
        consignor_id: int,
        deal_type: str = "consignment",
        payout_method: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Open a new intake order for a consignor."""
        if deal_type not in DEAL_TYPES:
            raise ValueError(f"deal_type must be one of {DEAL_TYPES}, got {deal_type!r}")
        if payout_method and payout_method not in PAYOUT_METHODS:
            raise ValueError(
                f"payout_method must be one of {PAYOUT_METHODS}, got {payout_method!r}"
            )
        values: dict[str, Any] = {
            "consignor_id": consignor_id,
            "deal_type": deal_type,
        }
        if payout_method:
            values["payout_method"] = payout_method
        values.update(extra)
        record = self.create(values)
        return {
            "summary": f"Consignment order {record['reference']} opened "
                       f"({deal_type})",
            "order": record,
        }

    # ── Items ────────────────────────────────────────────────────────

    def get_items(self, order_id: int, limit: int = 200) -> list[dict]:
        """Items on an intake order."""
        self._require()
        return self.client.search_read(
            self.ITEM_MODEL, [["order_id", "=", order_id]],
            fields=_ITEM_LIST_FIELDS, limit=limit,
        )

    def get_item(self, item_id: int) -> dict:
        """Read one item in detail."""
        self._require()
        rows = self.client.read(self.ITEM_MODEL, [item_id], fields=_ITEM_DETAIL_FIELDS)
        if not rows:
            from ..errors import OdooRecordNotFoundError
            raise OdooRecordNotFoundError(f"No consignment.item with id {item_id}")
        return rows[0]

    def items_in_state(self, state: str, limit: int = 100) -> list[dict]:
        """Items sitting in a given lifecycle state."""
        if state not in ITEM_STATES:
            raise ValueError(f"state must be one of {ITEM_STATES}, got {state!r}")
        self._require()
        return self.client.search_read(
            self.ITEM_MODEL, [["state", "=", state]],
            fields=_ITEM_LIST_FIELDS, limit=limit,
        )

    def items_awaiting_payout(self, limit: int = 100) -> list[dict]:
        """Sold items whose consignor has not been paid yet."""
        self._require()
        return self.client.search_read(
            self.ITEM_MODEL,
            [["state", "in", ["sold", "payout_pending", "ready_to_pay"]]],
            fields=_ITEM_DETAIL_FIELDS, limit=limit,
        )

    def add_item(
        self,
        order_id: int,
        brand_model: str,
        estimated_value: Optional[float] = None,
        condition_grade: Optional[str] = None,
        serial_no: Optional[str] = None,
        specs: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Add an item to an intake order."""
        if condition_grade and condition_grade not in GRADES:
            raise ValueError(f"condition_grade must be one of {GRADES}, got {condition_grade!r}")
        self._require()
        values: dict[str, Any] = {
            "order_id": order_id,
            "brand_model": brand_model,
        }
        if estimated_value is not None:
            values["estimated_value"] = float(estimated_value)
        if condition_grade:
            values["condition_grade"] = condition_grade
        if serial_no:
            values["serial_no"] = serial_no
        if specs:
            values["specs"] = specs
        values.update(extra)
        item_id = self.client.create(self.ITEM_MODEL, values)
        record = self.get_item(item_id)
        return {
            "summary": f"Item {record.get('reference') or item_id} "
                       f"({brand_model}) added to order {order_id}",
            "item": record,
        }

    def set_pricing(
        self,
        item_id: int,
        list_price: float,
        min_price: Optional[float] = None,
        split_pct: Optional[float] = None,
    ) -> dict:
        """Set the asking price (and optionally floor / split) on an item."""
        self._require()
        values: dict[str, Any] = {"list_price": float(list_price)}
        if min_price is not None:
            values["min_price"] = float(min_price)
        if split_pct is not None:
            values["split_pct"] = float(split_pct)
        self.client.write(self.ITEM_MODEL, item_id, values)
        record = self.get_item(item_id)
        return {
            "summary": f"Item {record.get('reference') or item_id} priced at "
                       f"{list_price}" + (f" (floor {min_price})" if min_price else ""),
            "item": record,
        }

    def run_item_action(self, item_id: int, method: str, **kwargs: Any) -> dict:
        """Invoke an allowlisted button method on a ``consignment.item``."""
        self._require()
        if method not in self.ALLOWED_ITEM_ACTIONS:
            from ._base import OdooActionNotAllowedError
            raise OdooActionNotAllowedError(
                f"Method '{method}' is not permitted on {self.ITEM_MODEL}. "
                f"Allowed: {', '.join(sorted(self.ALLOWED_ITEM_ACTIONS))}"
            )
        raw = self.client.execute(self.ITEM_MODEL, method, [item_id], **kwargs)
        return {
            "model": self.ITEM_MODEL,
            "id": item_id,
            "method": method,
            "returned": raw if not isinstance(raw, dict) else {
                "res_model": raw.get("res_model"), "res_id": raw.get("res_id"),
            },
            "record": self.get_item(item_id),
        }

    # ── Summary ──────────────────────────────────────────────────────

    def pipeline_summary(self) -> dict:
        """Item counts grouped into lifecycle phases."""
        self._require()
        by_state = {
            s: self.client.search_count(self.ITEM_MODEL, [["state", "=", s]])
            for s in ITEM_STATES
        }
        by_phase = {
            phase: sum(by_state[s] for s in states)
            for phase, states in ITEM_PHASES.items()
        }
        return {
            "summary": (
                f"Consignment: {by_phase['incoming']} incoming, "
                f"{by_phase['ready_to_sell']} ready to list, "
                f"{by_phase['on_market']} listed, "
                f"{by_phase['settling']} awaiting payout"
            ),
            "by_phase": by_phase,
            "by_state": by_state,
        }
