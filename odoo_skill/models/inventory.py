"""
Inventory and product operations for Odoo.

Covers ``product.product``, ``product.template``, and ``stock.quant``.
"""

import logging
from typing import Any, Optional

from ..client import OdooClient

logger = logging.getLogger("odoo_skill")

_PRODUCT_LIST_FIELDS = [
    "id", "name", "default_code", "barcode",
    "list_price", "standard_price",
    "type", "categ_id", "active",
]

_PRODUCT_DETAIL_FIELDS = _PRODUCT_LIST_FIELDS + [
    "qty_available", "virtual_available",
    "incoming_qty", "outgoing_qty",
    "description_sale",
    "uom_id", "weight", "volume",
]

_STOCK_FIELDS = [
    "id", "product_id", "location_id",
    "quantity", "reserved_quantity",
]


class InventoryOps:
    """High-level operations for Odoo products and stock levels.

    Args:
        client: An authenticated :class:`OdooClient` instance.
    """

    PRODUCT_MODEL = "product.product"
    QUANT_MODEL = "stock.quant"
    PICKING_MODEL = "stock.picking"
    MOVE_MODEL = "stock.move"
    MOVE_LINE_MODEL = "stock.move.line"
    SCRAP_MODEL = "stock.scrap"
    LOCATION_MODEL = "stock.location"
    PICKING_TYPE_MODEL = "stock.picking.type"

    #: The Recycling_Workbench staging location an item is moved into when it
    #: is sent to recycling (resolved by name at runtime; this is the fallback).
    _RECYCLING_LOC_FALLBACK = 422

    def __init__(self, client: OdooClient) -> None:
        self.client = client
        self._loc_cache: dict[str, int] = {}

    # ── Product search ───────────────────────────────────────────────

    def search_products(
        self,
        query: str,
        product_type: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search products by name or internal reference (SKU).

        Args:
            query: Search text (matched against name and default_code).
            product_type: Filter by type: ``'consu'``, ``'product'``,
                or ``'service'``. ``None`` returns all types.
            limit: Max results.

        Returns:
            List of matching product records.
        """
        domain: list = [
            "|",
            ["name", "ilike", query],
            ["default_code", "ilike", query],
        ]
        if product_type:
            domain.append(["type", "=", product_type])

        return self.client.search_read(
            self.PRODUCT_MODEL, domain,
            fields=_PRODUCT_LIST_FIELDS, limit=limit,
        )

    # ── Stock queries ────────────────────────────────────────────────

    def check_product_availability(self, product_id: int) -> dict:
        """Check stock and forecast for a single product.

        Args:
            product_id: The ``product.product`` ID.

        Returns:
            Dict with ``product``, ``sku``, ``on_hand``, ``forecasted``,
            ``incoming``, ``outgoing``.
        """
        records = self.client.read(
            self.PRODUCT_MODEL, product_id, fields=_PRODUCT_DETAIL_FIELDS,
        )
        if not records:
            return {}
        p = records[0]
        return {
            "id": p["id"],
            "product": p["name"],
            "sku": p.get("default_code") or "",
            "on_hand": p["qty_available"],
            "forecasted": p["virtual_available"],
            "incoming": p.get("incoming_qty", 0),
            "outgoing": p.get("outgoing_qty", 0),
            "unit_price": p["list_price"],
        }

    def get_stock_levels(
        self,
        product_id: Optional[int] = None,
        warehouse_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get current stock quantities from ``stock.quant``.

        Args:
            product_id: Filter by product.
            warehouse_id: Filter by warehouse.
            limit: Max results.

        Returns:
            List of stock quant records.
        """
        domain: list = [["location_id.usage", "=", "internal"]]
        if product_id:
            domain.append(["product_id", "=", product_id])
        if warehouse_id:
            domain.append(["warehouse_id", "=", warehouse_id])

        return self.client.search_read(
            self.QUANT_MODEL, domain, fields=_STOCK_FIELDS, limit=limit,
        )

    def get_low_stock_products(
        self,
        threshold: float = 10.0,
        limit: int = 50,
    ) -> list[dict]:
        """Find storable products whose on-hand quantity is at or below a threshold.

        Note:
            ``qty_available`` is a computed (non-stored) field in Odoo 19+,
            so we cannot filter/sort on it via domain. Instead we fetch all
            storable products and filter client-side.

        Args:
            threshold: Stock level threshold (default 10).
            limit: Max results.

        Returns:
            List of low-stock product records, sorted by qty ascending.
        """
        # Fetch all active storable products (type='product' in older Odoo,
        # but Odoo 19 uses 'consu' for consumable — fetch both)
        domain: list = [
            ["type", "in", ["product", "consu"]],
            ["active", "=", True],
        ]
        fields = ["id", "name", "default_code", "qty_available", "virtual_available", "list_price"]
        all_products = self.client.search_read(
            self.PRODUCT_MODEL, domain, fields=fields, limit=500,
        )
        # Filter client-side
        low_stock = [p for p in all_products if p.get("qty_available", 0) <= threshold]
        low_stock.sort(key=lambda p: p.get("qty_available", 0))
        return low_stock[:limit]

    # ── Recycling: send to the workbench, then write off ─────────────
    #
    # AndersonTech's floor flow is two stock steps:
    #   1. send_to_recycling — an internal transfer of the item out of its bin
    #      into the Recycling_Workbench staging location. It is still on hand,
    #      now parked at the workbench for dismantle/weigh.
    #   2. complete_recycling — a stock scrap out of the workbench into the
    #      Virtual/Scrap location. That is the write-off: it leaves inventory.
    # This mirrors how the warehouse already scraps (source → Virtual/Scrap),
    # with the workbench as the visible staging step in between.

    def _recycling_location_id(self) -> int:
        """The Recycling_Workbench staging location id (resolved by name)."""
        if "recycling" not in self._loc_cache:
            rows = self.client.search_read(
                self.LOCATION_MODEL,
                [["usage", "=", "internal"], ["complete_name", "ilike", "Recycling_Workbench"]],
                fields=["id"], limit=1,
            )
            self._loc_cache["recycling"] = rows[0]["id"] if rows else self._RECYCLING_LOC_FALLBACK
        return self._loc_cache["recycling"]

    def _scrap_void_location_id(self) -> int:
        """The write-off scrap location (a scrap location of usage 'inventory').

        Excludes the Recycling_Workbench, which is itself flagged a scrap
        location but is an *internal* staging spot, not the void.
        """
        if "void" not in self._loc_cache:
            rows = self.client.search_read(
                self.LOCATION_MODEL,
                [["scrap_location", "=", True], ["usage", "=", "inventory"]],
                fields=["id"], limit=1,
            )
            if not rows:
                raise ValueError("No scrap (write-off) location found in Odoo.")
            self._loc_cache["void"] = rows[0]["id"]
        return self._loc_cache["void"]

    def _stock_by_location(self, product_id: int, location_id: Optional[int] = None,
                           lot_id: Optional[int] = None) -> list[dict]:
        """On-hand quants for a product, optionally at one location / lot."""
        domain: list = [["product_id", "=", product_id], ["quantity", ">", 0]]
        if location_id is not None:
            domain.append(["location_id", "=", location_id])
        else:
            domain.append(["location_id.usage", "=", "internal"])
        if lot_id is not None:
            domain.append(["lot_id", "=", lot_id])
        return self.client.search_read(
            self.QUANT_MODEL, domain,
            fields=["location_id", "quantity", "lot_id"], limit=50,
        )

    def recycling_contents(self, limit: int = 100) -> list[dict]:
        """List what is currently staged in the Recycling_Workbench — read-only.

        The queue :meth:`complete_recycling` writes off. Use it to show the
        human what is waiting before completing anything.

        Returns:
            List of ``{product_id, product, quantity, lot_id}`` in the
            workbench.
        """
        rec = self._recycling_location_id()
        quants = self.client.search_read(
            self.QUANT_MODEL, [["location_id", "=", rec], ["quantity", ">", 0]],
            fields=["product_id", "quantity", "lot_id"], limit=limit,
        )
        return [
            {"product_id": q["product_id"][0], "product": q["product_id"][1],
             "quantity": q["quantity"],
             "lot_id": (q["lot_id"][0] if q.get("lot_id") else None)}
            for q in quants
        ]

    def send_to_recycling(self, product_id: int, quantity: float,
                          source_location_id: Optional[int] = None,
                          lot_id: Optional[int] = None) -> dict:
        """Move stock out of its bin into the Recycling_Workbench (step 1).

        Creates and validates an internal transfer for ``quantity`` of the
        product from its current bin to the workbench. The stock stays on hand
        (now at the workbench) until :meth:`complete_recycling` writes it off.

        Args:
            product_id: The ``product.product`` to recycle.
            quantity: How many units to send.
            source_location_id: Bin to pull from. If omitted, resolved from the
                product's on-hand quants — but only when it sits in exactly one
                internal location; otherwise the candidates are returned and the
                caller must pass one (never guess which bin).
            lot_id: Specific lot/serial, when the product is tracked.

        Returns:
            ``{product, quantity, from, to, picking, state, on_hand_workbench}``
            on success, or ``{needs_source, candidates}`` when the bin is
            ambiguous.
        """
        rec = self._recycling_location_id()

        if source_location_id is None:
            quants = self._stock_by_location(product_id, lot_id=lot_id)
            quants = [q for q in quants if q["location_id"][0] != rec]
            locs = {q["location_id"][0]: q["location_id"][1] for q in quants}
            if not locs:
                raise ValueError(
                    f"Product {product_id} has no on-hand stock to recycle.")
            if len(locs) > 1:
                return {"needs_source": True,
                        "candidates": [{"location_id": lid, "location": name,
                                        "on_hand": sum(q["quantity"] for q in quants
                                                       if q["location_id"][0] == lid)}
                                       for lid, name in locs.items()]}
            source_location_id = next(iter(locs))

        prod = self.client.read(self.PRODUCT_MODEL, product_id,
                                fields=["name", "uom_id"])
        if not prod:
            raise ValueError(f"Product {product_id} not found.")
        prod = prod[0]

        picking_type = self.client.search_read(
            self.PICKING_TYPE_MODEL,
            [["code", "=", "internal"]], fields=["id"], limit=1,
        )
        if not picking_type:
            raise ValueError("No internal-transfer operation type configured.")

        move = (0, 0, {
            "name": f"Recycle: {prod['name']}",
            "product_id": product_id,
            "product_uom_qty": quantity,
            "product_uom": prod["uom_id"][0],
            "location_id": source_location_id,
            "location_dest_id": rec,
        })
        picking_id = self.client.create(self.PICKING_MODEL, {
            "picking_type_id": picking_type[0]["id"],
            "location_id": source_location_id,
            "location_dest_id": rec,
            "move_ids_without_package": [move],
            "origin": "Recycling",
        })
        self.client.execute(self.PICKING_MODEL, "action_confirm", [picking_id])
        self.client.execute(self.PICKING_MODEL, "action_assign", [picking_id])

        state = self.client.read(self.PICKING_MODEL, picking_id, fields=["state"])[0]["state"]
        if state != "assigned":
            return {"product": prod["name"], "quantity": quantity,
                    "from": source_location_id, "to": rec, "picking": picking_id,
                    "state": state,
                    "blocked": f"could not reserve stock (state {state}); check the bin has {quantity} on hand"}

        for move_id in self.client.read(self.PICKING_MODEL, picking_id,
                                        fields=["move_ids"])[0]["move_ids"]:
            lines = self.client.search_read(
                self.MOVE_LINE_MODEL, [["move_id", "=", move_id]],
                fields=["id", "quantity", "lot_id"], limit=50,
            )
            if lot_id is not None:
                # Serial/lot tracked: pin the exact unit. action_assign may
                # have reserved a different serial, so force ours onto one line
                # and zero any others.
                if lines:
                    self.client.write(self.MOVE_LINE_MODEL, lines[0]["id"],
                                      {"lot_id": lot_id, "quantity": quantity})
                    for extra in lines[1:]:
                        self.client.write(self.MOVE_LINE_MODEL, extra["id"], {"quantity": 0})
                else:
                    self.client.create(self.MOVE_LINE_MODEL, {
                        "move_id": move_id, "product_id": product_id,
                        "location_id": source_location_id, "location_dest_id": rec,
                        "lot_id": lot_id, "quantity": quantity,
                    })
            elif lines:
                for ln in lines:
                    self.client.write(self.MOVE_LINE_MODEL, ln["id"],
                                      {"quantity": ln.get("quantity") or quantity})
            else:
                # Untracked with no reservation line — set the done qty on the move.
                self.client.write(self.MOVE_MODEL, move_id, {"quantity": quantity})
            self.client.write(self.MOVE_MODEL, move_id, {"picked": True})
        result = self.client.execute(self.PICKING_MODEL, "button_validate", [picking_id])
        if isinstance(result, dict):
            return {"product": prod["name"], "picking": picking_id,
                    "blocked": f"needs manual step: {result.get('res_model') or 'wizard'}"}

        final_state = self.client.read(self.PICKING_MODEL, picking_id, fields=["state"])[0]["state"]
        on_hand = sum(q["quantity"] for q in self._stock_by_location(product_id, rec))
        logger.info("send_to_recycling %s x%s → workbench (picking %d, %s)",
                    prod["name"], quantity, picking_id, final_state)
        return {"product": prod["name"], "quantity": quantity,
                "from": source_location_id, "to": rec, "picking": picking_id,
                "state": final_state, "on_hand_workbench": on_hand}

    def complete_recycling(self, product_id: int, quantity: Optional[float] = None,
                           lot_id: Optional[int] = None) -> dict:
        """Write off recycled stock out of the workbench (step 2).

        Scraps ``quantity`` of the product from the Recycling_Workbench to the
        Virtual/Scrap location — the item leaves inventory. Only stock already
        staged at the workbench can be completed; call :meth:`send_to_recycling`
        first. ``quantity`` defaults to everything staged for that product.

        Args:
            product_id: The ``product.product`` to write off.
            quantity: Units to scrap. Defaults to the full workbench on-hand.
            lot_id: Specific lot/serial, when tracked.

        Returns:
            ``{product, quantity, scrap, state, remaining_workbench}``.
        """
        rec = self._recycling_location_id()
        void = self._scrap_void_location_id()

        staged = self._stock_by_location(product_id, rec, lot_id=lot_id)
        available = sum(q["quantity"] for q in staged)
        if available <= 0:
            raise ValueError(
                f"Product {product_id} has nothing staged in the "
                f"Recycling_Workbench to complete. Send it to recycling first.")
        if quantity is None:
            quantity = available
        if quantity > available:
            raise ValueError(
                f"Only {available} of product {product_id} is staged in the "
                f"workbench; cannot complete {quantity}.")

        prod = self.client.read(self.PRODUCT_MODEL, product_id, fields=["name", "uom_id"])[0]
        vals = {
            "product_id": product_id,
            "scrap_qty": quantity,
            "product_uom_id": prod["uom_id"][0],
            "location_id": rec,
            "scrap_location_id": void,
        }
        if lot_id is not None:
            vals["lot_id"] = lot_id
        elif len(staged) == 1 and staged[0].get("lot_id"):
            vals["lot_id"] = staged[0]["lot_id"][0]

        scrap_id = self.client.create(self.SCRAP_MODEL, vals)
        result = self.client.execute(self.SCRAP_MODEL, "action_validate", [scrap_id])
        if isinstance(result, dict):
            # Insufficient-qty warning or another wizard — leave it draft.
            return {"product": prod["name"], "scrap": scrap_id,
                    "blocked": f"needs manual step: {result.get('res_model') or 'wizard'}"}

        state = self.client.read(self.SCRAP_MODEL, scrap_id, fields=["state", "name"])[0]
        remaining = sum(q["quantity"] for q in self._stock_by_location(product_id, rec))
        logger.info("complete_recycling %s x%s from workbench → scrap (%s, %s)",
                    prod["name"], quantity, state["name"], state["state"])
        return {"product": prod["name"], "quantity": quantity,
                "scrap": state["name"], "state": state["state"],
                "remaining_workbench": remaining}
