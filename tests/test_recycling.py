"""Tests for the recycling flow (``inventory.send_to_recycling`` /
``complete_recycling`` / ``recycling_contents``).

These lock the two-step floor flow a live test on prod inventory is too risky
to exercise repeatedly:

* Send is an internal transfer of the item's bin stock into the workbench; it
  reserves, sets the done quantity on the move line, then native
  ``button_validate``.
* For a serial/lot-tracked item send pins the *exact* serial on the move line —
  even if reservation grabbed a different one — so the right unit moves.
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

    ``quants`` is the source of truth. A validated transfer reads the move
    line's (possibly corrected) lot + quantity and moves that into REC; a
    validated scrap removes staged stock from REC. ``reserve_lot`` lets a test
    make action_assign reserve a *wrong* serial, to prove send forces the right
    one onto the move line.
    """

    def __init__(self, quants, reserve_lot=None):
        self.quants = quants
        self.reserve_lot = reserve_lot
        self.created = []
        self.line_writes = []
        self.move_writes = []
        self.validated_pickings = []
        self.validated_scraps = []
        self._picking_state = {}
        self._picking_moves = {}
        self._move_info = {}
        self._move_lines = {}
        self._seq = 1000

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

    def search_read(self, model, domain, fields=None, limit=None, **kw):
        if model == "stock.location":
            crit = {d[0]: d[2] for d in domain if isinstance(d, list)}
            if "Recycling_Workbench" in str(crit.values()):
                return [{"id": REC}]
            if crit.get("scrap_location") is True:
                return [{"id": VOID}]
            return []
        if model == "stock.picking.type":
            return [{"id": 5}]
        if model == "stock.move.line":
            move_id = next(d[2] for d in domain if d[0] == "move_id")
            return [dict(ln) for ln in self._move_lines.get(move_id, [])]
        if model == "stock.quant":
            product_id = location_id = lot_id = None
            for d in domain:
                if not isinstance(d, list):
                    continue
                if d[0] == "product_id":
                    product_id = d[2]
                elif d[0] == "location_id":
                    location_id = d[2]
                elif d[0] == "lot_id":
                    lot_id = d[2]
            return [{"product_id": [q["product_id"], "Dell OptiPlex 3080"],
                     "location_id": [q["location_id"], f"L{q['location_id']}"],
                     "quantity": q["quantity"],
                     "lot_id": ([q["lot_id"], "SERIAL"] if q.get("lot_id") else False)}
                    for q in self._q_for(product_id, location_id, lot_id)]
        raise AssertionError(f"unexpected search_read {model} {domain}")

    def read(self, model, ids, fields=None):
        i = ids[0] if isinstance(ids, list) else ids
        if model == "product.product":
            return [{"id": i, "name": "Dell OptiPlex 3080", "uom_id": [1, "Units"]}]
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
            move_id = self._next()
            mv = vals["move_ids_without_package"][0][2]
            self._picking_moves[rid] = [move_id]
            self._move_info[move_id] = {"product_id": mv["product_id"],
                                        "src": mv["location_id"],
                                        "qty": mv["product_uom_qty"]}
        if model == "stock.move.line":
            self._move_lines.setdefault(vals["move_id"], []).append(
                {"id": rid, "quantity": vals.get("quantity", 0),
                 "lot_id": [vals["lot_id"], "S"] if vals.get("lot_id") else False})
        return rid

    def write(self, model, ids, values):
        if model == "stock.move":
            self.move_writes.append((ids, values))
        if model == "stock.move.line":
            self.line_writes.append((ids, values))
            for lines in self._move_lines.values():
                for ln in lines:
                    if ln["id"] == ids:
                        if "quantity" in values:
                            ln["quantity"] = values["quantity"]
                        if "lot_id" in values:
                            ln["lot_id"] = [values["lot_id"], "S"]
        return True

    def execute(self, model, method, ids, *a, **k):
        pid = ids[0] if isinstance(ids, list) else ids
        if model == "stock.picking" and method == "action_confirm":
            return True
        if model == "stock.picking" and method == "action_assign":
            for move_id in self._picking_moves[pid]:
                info = self._move_info[move_id]
                self._move_lines[move_id] = [{
                    "id": self._next(), "quantity": info["qty"],
                    "lot_id": ([self.reserve_lot, "S"] if self.reserve_lot else False),
                }]
            self._picking_state[pid] = "assigned"
            return True
        if model == "stock.picking" and method == "button_validate":
            self._picking_state[pid] = "done"
            self.validated_pickings.append(pid)
            for move_id in self._picking_moves[pid]:
                info = self._move_info[move_id]
                line = self._move_lines[move_id][0]
                qty = line["quantity"]
                lot = line["lot_id"][0] if line.get("lot_id") else None
                for q in self.quants:
                    if q["product_id"] == info["product_id"] and q["location_id"] == info["src"]:
                        q["quantity"] -= qty
                        break
                self.quants.append({"product_id": info["product_id"],
                                    "location_id": REC, "quantity": qty, "lot_id": lot})
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


def _ops(quants, reserve_lot=None):
    ops = InventoryOps(FakeStock(quants, reserve_lot))
    return ops, ops.client


def test_send_moves_bin_stock_into_the_workbench():
    ops, fake = _ops([{"product_id": 99, "location_id": BIN_A, "quantity": 5, "lot_id": None}])
    res = ops.send_to_recycling(99, 2)
    assert res["to"] == REC and res["from"] == BIN_A and res["state"] == "done"
    pick = next(v for m, v in fake.created if m == "stock.picking")
    assert pick["location_dest_id"] == REC
    assert fake.validated_pickings
    assert any(vals.get("picked") is True for _i, vals in fake.move_writes)
    assert res["on_hand_workbench"] == 2


def test_send_pins_the_exact_serial_even_if_reservation_grabbed_another():
    # reservation grabs serial 700, but we asked for lot 42 → it must be forced.
    ops, fake = _ops(
        [{"product_id": 99, "location_id": BIN_A, "quantity": 1, "lot_id": 42}],
        reserve_lot=700,
    )
    res = ops.send_to_recycling(99, 1, lot_id=42)
    assert res["state"] == "done"
    # the move line was rewritten to our lot
    assert any(vals.get("lot_id") == 42 for _i, vals in fake.line_writes)
    # and the unit that landed in the workbench carries lot 42
    rec_q = [q for q in fake.quants if q["location_id"] == REC]
    assert rec_q and rec_q[0]["lot_id"] == 42


def test_send_refuses_to_guess_between_bins():
    ops, fake = _ops([
        {"product_id": 99, "location_id": BIN_A, "quantity": 3, "lot_id": None},
        {"product_id": 99, "location_id": BIN_B, "quantity": 1, "lot_id": None},
    ])
    res = ops.send_to_recycling(99, 1)
    assert res.get("needs_source") is True
    assert {c["location_id"] for c in res["candidates"]} == {BIN_A, BIN_B}
    assert not fake.created


def test_send_with_no_stock_raises():
    ops, _ = _ops([])
    with pytest.raises(ValueError, match="no on-hand stock"):
        ops.send_to_recycling(99, 1)


def test_complete_scraps_from_workbench_to_void():
    ops, fake = _ops([{"product_id": 99, "location_id": REC, "quantity": 4, "lot_id": None}])
    res = ops.complete_recycling(99)
    scrap = next(v for m, v in fake.created if m == "stock.scrap")
    assert scrap["location_id"] == REC and scrap["scrap_location_id"] == VOID
    assert scrap["scrap_qty"] == 4 and fake.validated_scraps
    assert res["remaining_workbench"] == 0


def test_complete_pins_the_staged_serial():
    ops, fake = _ops([{"product_id": 99, "location_id": REC, "quantity": 1, "lot_id": 42}])
    ops.complete_recycling(99)
    scrap = next(v for m, v in fake.created if m == "stock.scrap")
    assert scrap.get("lot_id") == 42


def test_complete_with_nothing_staged_raises():
    ops, fake = _ops([{"product_id": 99, "location_id": BIN_A, "quantity": 9, "lot_id": None}])
    with pytest.raises(ValueError, match="nothing staged"):
        ops.complete_recycling(99)
    assert not fake.created


def test_complete_refuses_more_than_staged():
    ops, _ = _ops([{"product_id": 99, "location_id": REC, "quantity": 2, "lot_id": None}])
    with pytest.raises(ValueError, match="cannot complete"):
        ops.complete_recycling(99, quantity=5)


def test_contents_lists_workbench_without_writing():
    ops, fake = _ops([{"product_id": 99, "location_id": REC, "quantity": 4, "lot_id": None}])
    rows = ops.recycling_contents()
    assert rows == [{"product_id": 99, "product": "Dell OptiPlex 3080", "quantity": 4, "lot_id": None}]
    assert not fake.created
