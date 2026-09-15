"""Tests for the recycling flow (``inventory.send_to_recycling`` /
``complete_recycling`` / ``recycling_contents``).

These lock the two-step floor flow a live test on prod inventory is too risky
to exercise repeatedly:

* Send is an internal transfer of the item's bin stock into the workbench; it
  sets the move's done quantity + picked, then native ``button_validate``.
* Send refuses to guess the bin: with the product in more than one location and
  no ``source_location_id``, it returns the candidates and writes nothing.
* Complete scraps only what is staged in the workbench, from the workbench to
  the void scrap location; nothing staged raises rather than scrapping.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.models.inventory import InventoryOps  # noqa: E402

REC = 422   # Recycling_Workbench
VOID = 16   # Virtual Locations/Scrap
BIN_A = 29
BIN_B = 34


class FakeStock:
    """Minimal stateful Odoo stand-in for the recycling ops.

    Models just enough of stock.location / stock.quant / stock.picking /
    stock.move / stock.scrap for the flow. ``quants`` is the source of truth;
    a validated transfer moves qty into REC, a validated scrap removes it.
    """

    def __init__(self, quants):
        # quants: list of {product_id, location_id, quantity, lot_id}
        self.quants = quants
        self.created = []        # (model, vals)
        self.move_writes = []
        self.validated_pickings = []
        self.validated_scraps = []
        self._picking_state = {}
        self._picking_moves = {}
        self._seq = 1000

    # helpers ------------------------------------------------------------
    def _next(self):
        self._seq += 1
        return self._seq

    def _q_for(self, product_id, location_id=None, lot_id=None):
        out = []
        for q in self.quants:
            if q["quantity"] <= 0:
                continue
            if product_id is not None and q["product_id"] != product_id:
                continue
            if location_id is not None and q["location_id"] != location_id:
                continue
            if lot_id is not None and q.get("lot_id") != lot_id:
                continue
            out.append(q)
        return out

    # OdooClient surface -------------------------------------------------
    def search_read(self, model, domain, fields=None, limit=None, **kw):
        if model == "stock.location":
            crit = {d[0]: d[2] for d in domain if isinstance(d, list)}
            if crit.get("complete_name") == "Recycling_Workbench" or \
               "Recycling_Workbench" in str(crit.values()):
                return [{"id": REC}]
            if crit.get("scrap_location") is True:
                return [{"id": VOID}]
            return []
        if model == "stock.picking.type":
            return [{"id": 5}]
        if model == "stock.quant":
            # parse the product / location / lot out of the domain
            product_id = location_id = lot_id = None
            usage_internal = False
            for d in domain:
                if not isinstance(d, list):
                    continue
                if d[0] == "product_id":
                    product_id = d[2]
                elif d[0] == "location_id":
                    location_id = d[2]
                elif d[0] == "location_id.usage":
                    usage_internal = True
                elif d[0] == "lot_id":
                    lot_id = d[2]
            rows = self._q_for(product_id, location_id, lot_id)
            return [{"product_id": [q["product_id"], "Junk Board"],
                     "location_id": [q["location_id"], f"L{q['location_id']}"],
                     "quantity": q["quantity"],
                     "lot_id": ([q["lot_id"], "LOT"] if q.get("lot_id") else False)}
                    for q in rows]
        raise AssertionError(f"unexpected search_read {model} {domain}")

    def read(self, model, ids, fields=None):
        i = ids[0] if isinstance(ids, list) else ids
        if model == "product.product":
            return [{"id": i, "name": "Junk Board", "uom_id": [1, "Units"]}]
        if model == "stock.picking":
            if "move_ids" in (fields or []):
                return [{"move_ids": self._picking_moves.get(i, [])}]
            return [{"state": self._picking_state.get(i, "assigned")}]
        if model == "stock.scrap":
            return [{"state": "done", "name": "SP/TEST"}]
        raise AssertionError(f"unexpected read {model}")

    def create(self, model, vals):
        rid = self._next()
        self.created.append((model, vals))
        if model == "stock.picking":
            self._picking_state[rid] = "confirmed"
            self._picking_moves[rid] = [self._next()]
        return rid

    def write(self, model, ids, values):
        if model == "stock.move":
            self.move_writes.append((ids, values))
        return True

    def execute(self, model, method, ids, *a, **k):
        pid = ids[0] if isinstance(ids, list) else ids
        if model == "stock.picking" and method == "action_confirm":
            return True
        if model == "stock.picking" and method == "action_assign":
            self._picking_state[pid] = "assigned"
            return True
        if model == "stock.picking" and method == "button_validate":
            self._picking_state[pid] = "done"
            self.validated_pickings.append(pid)
            # apply the move: pull from source bin into REC
            vals = dict(self.created[-1][1]) if self.created else {}
            mv = vals.get("move_ids_without_package", [(0, 0, {})])[0][2]
            src, qty, prod = mv["location_id"], mv["product_uom_qty"], mv["product_id"]
            for q in self.quants:
                if q["product_id"] == prod and q["location_id"] == src:
                    q["quantity"] -= qty
                    break
            self.quants.append({"product_id": prod, "location_id": REC,
                                "quantity": qty, "lot_id": None})
            return True
        if model == "stock.scrap" and method == "action_validate":
            self.validated_scraps.append(pid)
            vals = self.created[-1][1]
            for q in self.quants:
                if q["product_id"] == vals["product_id"] and q["location_id"] == REC:
                    q["quantity"] -= vals["scrap_qty"]
                    break
            return True
        raise AssertionError(f"unexpected execute {model}.{method}")


def _ops(quants):
    ops = InventoryOps(FakeStock(quants))
    return ops, ops.client


def test_send_moves_bin_stock_into_the_workbench():
    ops, fake = _ops([{"product_id": 99, "location_id": BIN_A, "quantity": 5, "lot_id": None}])
    res = ops.send_to_recycling(99, 2)
    assert res["to"] == REC and res["from"] == BIN_A
    assert res["state"] == "done"
    # a transfer picking was created into REC and validated
    pick = next(v for m, v in fake.created if m == "stock.picking")
    assert pick["location_dest_id"] == REC
    assert fake.validated_pickings
    # move got its done qty + picked before validate
    assert any(vals == {"quantity": 2, "picked": True} for _i, vals in fake.move_writes)
    assert res["on_hand_workbench"] == 2


def test_send_refuses_to_guess_between_bins():
    ops, fake = _ops([
        {"product_id": 99, "location_id": BIN_A, "quantity": 3, "lot_id": None},
        {"product_id": 99, "location_id": BIN_B, "quantity": 1, "lot_id": None},
    ])
    res = ops.send_to_recycling(99, 1)
    assert res.get("needs_source") is True
    assert {c["location_id"] for c in res["candidates"]} == {BIN_A, BIN_B}
    assert not fake.created  # nothing written


def test_send_with_no_stock_raises():
    ops, _ = _ops([])
    with pytest.raises(ValueError, match="no on-hand stock"):
        ops.send_to_recycling(99, 1)


def test_complete_scraps_from_workbench_to_void():
    ops, fake = _ops([{"product_id": 99, "location_id": REC, "quantity": 4, "lot_id": None}])
    res = ops.complete_recycling(99)   # default qty = all staged
    scrap = next(v for m, v in fake.created if m == "stock.scrap")
    assert scrap["location_id"] == REC and scrap["scrap_location_id"] == VOID
    assert scrap["scrap_qty"] == 4
    assert fake.validated_scraps
    assert res["remaining_workbench"] == 0


def test_complete_with_nothing_staged_raises():
    ops, fake = _ops([{"product_id": 99, "location_id": BIN_A, "quantity": 9, "lot_id": None}])
    with pytest.raises(ValueError, match="nothing staged"):
        ops.complete_recycling(99)
    assert not fake.created  # never scrapped bin stock


def test_complete_refuses_more_than_staged():
    ops, _ = _ops([{"product_id": 99, "location_id": REC, "quantity": 2, "lot_id": None}])
    with pytest.raises(ValueError, match="cannot complete"):
        ops.complete_recycling(99, quantity=5)


def test_contents_lists_workbench_without_writing():
    ops, fake = _ops([{"product_id": 99, "location_id": REC, "quantity": 4, "lot_id": None}])
    rows = ops.recycling_contents()
    assert rows == [{"product_id": 99, "product": "Junk Board", "quantity": 4, "lot_id": None}]
    assert not fake.created
