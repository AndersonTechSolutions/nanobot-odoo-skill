"""Tests for the sale-order delivery flow (``sales.deliver_order`` + status).

These lock the behaviour a live smoke test on prod inventory is too risky to
exercise repeatedly:

* A draft quotation is never delivered — it raises, and nothing is written.
* The multi-step warehouse route (Pick → Pack → Out) is walked to completion:
  each transfer is validated only once its predecessor readies it, and every
  link ends ``done``.
* Each validated transfer sets the move's done ``quantity`` and ``picked``
  before the native ``button_validate`` — matching what a completed transfer
  looks like on the Odoo 17 DB.
* A transfer that cannot reserve its stock is reported as blocked and left
  untouched — never force-validated.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.models.sale_order import SaleOrderOps  # noqa: E402


class FakeChain:
    """A tiny stateful stand-in for the Pick → Pack → Out picking chain.

    Only the fields ``deliver_order`` reads/writes are modelled. Validating a
    ready ('assigned') picking marks it 'done' and readies the next link, so
    the loop must take several passes to drain the chain — exactly the prod
    shape that a single-pass validate would get wrong.
    """

    def __init__(self, order_state="sale", reservable=True):
        self.order = {"id": 1, "name": "S01706", "state": order_state,
                      "picking_ids": [10, 11, 12],
                      "delivery_status": "pending", "invoice_status": "no",
                      "task_id": False, "fsm_task_ids": []}
        # chain order: PICK(10) → PACK(11) → OUT(12)
        self.pickings = {
            10: {"id": 10, "name": "PICK/1", "state": "assigned",
                 "picking_type_code": "internal", "move_ids": [100]},
            11: {"id": 11, "name": "PACK/1", "state": "waiting",
                 "picking_type_code": "internal", "move_ids": [101]},
            12: {"id": 12, "name": "OUT/1", "state": "waiting",
                 "picking_type_code": "outgoing", "move_ids": [102]},
        }
        self._next = {10: 11, 11: 12, 12: None}
        self.moves = {100: {"product_uom_qty": 1.0},
                      101: {"product_uom_qty": 1.0},
                      102: {"product_uom_qty": 1.0}}
        self.move_writes = []
        self.reservable = reservable

    # OdooClient surface used by SaleOrderOps ----------------------------
    def read(self, model, ids, fields=None):
        if model == "sale.order":
            return [dict(self.order)]
        if model == "stock.picking":
            id_list = ids if isinstance(ids, list) else [ids]
            return [dict(self.pickings[i]) for i in id_list]
        if model == "stock.move":
            i = ids if not isinstance(ids, list) else ids[0]
            return [dict(self.moves[i])]
        raise AssertionError(f"unexpected read {model}")

    def write(self, model, ids, values):
        if model == "stock.move":
            self.move_writes.append((ids, values))
        return True

    def execute(self, model, method, ids, *a, **k):
        pid = ids[0] if isinstance(ids, list) else ids
        if method == "button_validate":
            self.pickings[pid]["state"] = "done"
            nxt = self._next[pid]
            if nxt is not None and self.pickings[nxt]["state"] == "waiting":
                # predecessor done → next link auto-reserves
                self.pickings[nxt]["state"] = "assigned" if self.reservable else "confirmed"
            self._recompute_status()
            return True
        if method == "action_assign":
            if self.reservable and self.pickings[pid]["state"] in ("waiting", "confirmed"):
                self.pickings[pid]["state"] = "assigned"
            return True
        raise AssertionError(f"unexpected execute {model}.{method}")

    def _recompute_status(self):
        out = self.pickings[12]
        self.order["delivery_status"] = "full" if out["state"] == "done" else "pending"
        if out["state"] == "done":
            self.order["invoice_status"] = "to invoice"


@pytest.fixture()
def sales_ops():
    return SaleOrderOps(MagicMock())


def test_draft_order_is_refused_and_nothing_written(sales_ops):
    chain = FakeChain(order_state="draft")
    sales_ops.client = chain
    with pytest.raises(ValueError, match="confirmed sale"):
        sales_ops.deliver_order(1)
    assert chain.move_writes == []
    assert chain.pickings[10]["state"] == "assigned"  # untouched


def test_full_chain_is_walked_to_done(sales_ops):
    chain = FakeChain()
    sales_ops.client = chain
    result = sales_ops.deliver_order(1)
    assert result["delivery_status"] == "full"
    assert result["delivered"] == ["PICK/1", "PACK/1", "OUT/1"]
    assert result["blocked"] == []
    assert all(p["state"] == "done" for p in chain.pickings.values())


def test_each_move_sets_quantity_and_picked(sales_ops):
    chain = FakeChain()
    sales_ops.client = chain
    sales_ops.deliver_order(1)
    # one write per move, each carrying the demand and picked=True
    assert len(chain.move_writes) == 3
    for _ids, vals in chain.move_writes:
        assert vals == {"quantity": 1.0, "picked": True}


def test_unreservable_link_is_reported_not_forced(sales_ops):
    # PICK validates, but PACK/OUT can never reserve → blocked, not shipped.
    chain = FakeChain(reservable=False)
    sales_ops.client = chain
    result = sales_ops.deliver_order(1)
    assert "PICK/1" in result["delivered"]
    assert result["delivery_status"] != "full"
    assert any(b["picking"] in ("PACK/1", "OUT/1") for b in result["blocked"])
    assert chain.pickings[12]["state"] != "done"  # OUT never force-validated


def test_status_read_reports_chain_without_writing(sales_ops):
    chain = FakeChain()
    sales_ops.client = chain
    status = sales_ops.get_delivery_status(1)
    assert status["delivery_status"] == "pending"
    assert [p["name"] for p in status["pickings"]] == ["PICK/1", "PACK/1", "OUT/1"]
    assert chain.move_writes == []  # read-only
