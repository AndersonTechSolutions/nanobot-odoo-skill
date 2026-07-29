"""
eBay listing and repricing operations for the ``sale_ebay`` fork.

Two models are in play:

* ``ebay.listing`` — a listing record that publishes to eBay's REST
  Inventory/Offer API via ``action_publish``. ``kind`` is ``single`` or
  ``multi`` (variant group).
* ``product.template`` — carries the eBay listing configuration
  (``ebay_title``, ``ebay_fixed_price``, category, policies) *and* the
  competitor-research fields that drive repricing.

**Repricing is proposal-first by design.** ``ebay_suggested_price`` and
``ebay_suggested_discount_pct`` are computed from researched comps, clamped
by a cost floor (``sale_ebay.reducer_min_margin``) and an anchor
(``sale_ebay.reducer_anchor``: low / p25 / median). Reading a suggestion is
free; applying it is a separate, explicit call. :meth:`EbayListingOps.apply_suggested_price`
refuses to act when the suggestion is absent or equal to the current price,
so an unattended worker cannot churn prices on stale data.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_LISTING_FIELDS = [
    "id", "name", "product_tmpl_id", "kind", "ebay_listing_status",
    "ebay_id", "ebay_offer_id", "ebay_url", "variant_count", "status_note",
]
_LISTING_DETAIL_FIELDS = _LISTING_FIELDS + [
    "ebay_category_id", "ebay_item_condition_id", "ebay_condition_description",
    "ebay_best_offer", "ebay_inventory_group_key",
    "ebay_seller_payment_policy_id", "ebay_seller_return_policy_id",
    "ebay_seller_shipping_policy_id", "variant_ids", "image_ids",
]

_PRODUCT_LIST_FIELDS = [
    "id", "name", "default_code", "list_price", "standard_price",
    "ebay_use", "ebay_listed", "ebay_title", "ebay_fixed_price",
    "ebay_quantity", "ebay_days_listed", "ebay_url",
]

_COMP_FIELDS = [
    "id", "name", "ebay_title", "ebay_fixed_price", "standard_price",
    "ebay_comp_count", "ebay_comp_low", "ebay_comp_p25", "ebay_comp_median",
    "ebay_comp_high", "ebay_comp_note", "ebay_comp_fetched_at",
    "ebay_suggested_price", "ebay_suggested_discount_pct",
    "ebay_days_listed", "ebay_listed", "ebay_url",
]

#: ebay.listing.ebay_listing_status values.
LISTING_STATUSES = ["draft", "active", "ended"]


class EbayListingOps(BaseOps):
    """Listing lifecycle and comp-driven repricing."""

    MODEL = "ebay.listing"
    MODULE = "sale_ebay"
    PRODUCT_MODEL = "product.template"
    LIST_FIELDS = _LISTING_FIELDS
    DETAIL_FIELDS = _LISTING_DETAIL_FIELDS
    ORDER = "id desc"

    ALLOWED_ACTIONS = frozenset({
        "action_publish",
        "action_end",
        "action_refresh_status",
    })

    #: Methods permitted on ``product.template`` via :meth:`run_product_action`.
    ALLOWED_PRODUCT_ACTIONS = frozenset({
        "action_ebay_research_comps",
        "action_list_on_ebay",
        "action_end_single_listing",
        "action_ebay_listing_per_variant",
    })

    # ── Listings ─────────────────────────────────────────────────────

    def active_listings(self, limit: int = 100) -> list[dict]:
        """Listings currently live on eBay."""
        return self.search([["ebay_listing_status", "=", "active"]], limit=limit)

    def draft_listings(self, limit: int = 100) -> list[dict]:
        """Listings staged but not yet published."""
        return self.search([["ebay_listing_status", "=", "draft"]], limit=limit)

    def listings_for_product(self, product_tmpl_id: int) -> list[dict]:
        """All listing records for a product template."""
        return self.search([["product_tmpl_id", "=", product_tmpl_id]], limit=50)

    def publish(self, listing_id: int) -> dict:
        """Publish a listing to eBay (live API call).

        This creates/updates the inventory item and offer on eBay. It is a
        real outward-facing action — the listing becomes publicly visible.
        """
        result = self.run_action(listing_id, "action_publish")
        record = result["record"]
        return {
            "summary": (
                f"Listing '{record.get('name')}' → "
                f"{record.get('ebay_listing_status')}"
                + (f" ({record['ebay_url']})" if record.get("ebay_url") else "")
                + (f" — {record['status_note']}" if record.get("status_note") else "")
            ),
            "listing": record,
        }

    def end_listing(self, listing_id: int) -> dict:
        """End a live eBay listing."""
        result = self.run_action(listing_id, "action_end")
        return {
            "summary": f"Listing {listing_id} ended "
                       f"({result['record'].get('ebay_listing_status')})",
            "listing": result["record"],
        }

    def create_listing(
        self,
        product_tmpl_id: int,
        name: str,
        **extra: Any,
    ) -> dict:
        """Stage a listing record for a product (does not publish).

        Publishing is a separate :meth:`publish` call so that staging and
        going live are never one step.
        """
        values: dict[str, Any] = {
            "product_tmpl_id": product_tmpl_id,
            "name": name,
        }
        values.update(extra)
        record = self.create(values)
        return {
            "summary": f"Listing '{name}' staged for product {product_tmpl_id} "
                       f"(status: {record.get('ebay_listing_status')}). "
                       f"Not yet published.",
            "listing": record,
        }

    # ── Repricing (proposal-first) ───────────────────────────────────

    def research_comps(self, product_tmpl_id: int) -> dict:
        """Refresh competitor comps for a product, then report the suggestion.

        Calls ``action_ebay_research_comps`` (an eBay Browse API search) and
        reads back the recomputed comp aggregates and suggested price.
        """
        self._require()
        self.client.execute(
            self.PRODUCT_MODEL, "action_ebay_research_comps", [product_tmpl_id]
        )
        return self.get_pricing(product_tmpl_id)

    def get_pricing(self, product_tmpl_id: int) -> dict:
        """Read the current comp aggregates and price suggestion for a product."""
        self._require()
        rows = self.client.read(
            self.PRODUCT_MODEL, [product_tmpl_id], fields=_COMP_FIELDS
        )
        if not rows:
            from ..errors import OdooRecordNotFoundError
            raise OdooRecordNotFoundError(
                f"No product.template with id {product_tmpl_id}"
            )
        p = rows[0]
        current = p.get("ebay_fixed_price") or 0.0
        suggested = p.get("ebay_suggested_price") or 0.0
        pct = p.get("ebay_suggested_discount_pct") or 0.0
        actionable = bool(suggested and current and abs(suggested - current) > 0.005)
        return {
            "summary": (
                f"{p.get('name')}: listed at {current:.2f}, "
                f"{p.get('ebay_comp_count') or 0} comps "
                f"(low {p.get('ebay_comp_low') or 0:.2f} / "
                f"p25 {p.get('ebay_comp_p25') or 0:.2f} / "
                f"median {p.get('ebay_comp_median') or 0:.2f}) → "
                + (f"suggest {suggested:.2f} (−{pct:.1f}%)" if actionable
                   else "no change suggested")
                + f". {p.get('ebay_comp_note') or ''}"
            ).strip(),
            "actionable": actionable,
            "current_price": current,
            "suggested_price": suggested,
            "discount_pct": pct,
            "note": p.get("ebay_comp_note"),
            "comp_count": p.get("ebay_comp_count"),
            "comps_fetched_at": p.get("ebay_comp_fetched_at"),
            "days_listed": p.get("ebay_days_listed"),
            "product": p,
        }

    def repricing_candidates(
        self, min_days_listed: int = 0, limit: int = 100
    ) -> list[dict]:
        """Listed products whose suggested price differs from the current one.

        This is the worker's work-list. ``min_days_listed`` filters to items
        that have had time to sell at the current price.
        """
        self._require()
        domain: list = [
            ["ebay_listed", "=", True],
            ["ebay_suggested_discount_pct", ">", 0],
        ]
        if min_days_listed:
            domain.append(["ebay_days_listed", ">=", min_days_listed])
        return self.client.search_read(
            self.PRODUCT_MODEL, domain, fields=_COMP_FIELDS,
            limit=limit, order="ebay_suggested_discount_pct desc",
        )

    def stale_comps(self, limit: int = 100) -> list[dict]:
        """Listed products whose comps have never been fetched.

        Feed these to :meth:`research_comps` before trusting any suggestion.
        """
        self._require()
        return self.client.search_read(
            self.PRODUCT_MODEL,
            [["ebay_listed", "=", True], ["ebay_comp_fetched_at", "=", False]],
            fields=_COMP_FIELDS, limit=limit,
        )

    def apply_suggested_price(
        self,
        product_tmpl_id: int,
        max_discount_pct: float = 25.0,
        confirm: bool = False,
    ) -> dict:
        """Apply the computed suggestion to ``ebay_fixed_price``.

        Guarded three ways, because this changes a live price:

        * refuses when there is no actionable suggestion;
        * refuses when the cut exceeds *max_discount_pct*;
        * requires ``confirm=True``, so a dry run is the default.

        The Odoo-side cost floor still applies underneath — a suggestion is
        never generated below ``standard_price × (1 + min_margin)``.
        """
        pricing = self.get_pricing(product_tmpl_id)
        if not pricing["actionable"]:
            return {
                "summary": f"No price change to apply — {pricing['note'] or 'suggestion matches current price'}.",
                "applied": False,
                "pricing": pricing,
            }
        pct = pricing["discount_pct"]
        if pct > max_discount_pct:
            return {
                "summary": (
                    f"Refused: suggested cut of {pct:.1f}% exceeds the "
                    f"{max_discount_pct:.1f}% ceiling. Raise max_discount_pct "
                    f"to override."
                ),
                "applied": False,
                "pricing": pricing,
            }
        if not confirm:
            return {
                "summary": (
                    f"Dry run — would change {pricing['product'].get('name')} "
                    f"from {pricing['current_price']:.2f} to "
                    f"{pricing['suggested_price']:.2f} (−{pct:.1f}%). "
                    f"Pass confirm=True to apply."
                ),
                "applied": False,
                "pricing": pricing,
            }
        self.client.write(
            self.PRODUCT_MODEL, product_tmpl_id,
            {"ebay_fixed_price": pricing["suggested_price"]},
        )
        after = self.get_pricing(product_tmpl_id)
        return {
            "summary": (
                f"Price applied: {pricing['product'].get('name')} "
                f"{pricing['current_price']:.2f} → {pricing['suggested_price']:.2f} "
                f"(−{pct:.1f}%)"
            ),
            "applied": True,
            "before": pricing,
            "after": after,
        }

    def run_product_action(
        self, product_tmpl_id: int, method: str, **kwargs: Any
    ) -> dict:
        """Invoke an allowlisted eBay method on a ``product.template``."""
        self._require()
        if method not in self.ALLOWED_PRODUCT_ACTIONS:
            from ._base import OdooActionNotAllowedError
            raise OdooActionNotAllowedError(
                f"Method '{method}' is not permitted on {self.PRODUCT_MODEL}. "
                f"Allowed: {', '.join(sorted(self.ALLOWED_PRODUCT_ACTIONS))}"
            )
        raw = self.client.execute(self.PRODUCT_MODEL, method, [product_tmpl_id], **kwargs)
        rows = self.client.read(
            self.PRODUCT_MODEL, [product_tmpl_id], fields=_PRODUCT_LIST_FIELDS
        )
        return {
            "model": self.PRODUCT_MODEL,
            "id": product_tmpl_id,
            "method": method,
            "returned": raw if not isinstance(raw, dict) else {
                "res_model": raw.get("res_model"), "res_id": raw.get("res_id"),
            },
            "record": rows[0] if rows else None,
        }

    # ── Summary ──────────────────────────────────────────────────────

    def listing_summary(self) -> dict:
        """Listing counts plus the size of the repricing work-list."""
        by_status = {
            s: self.count([["ebay_listing_status", "=", s]])
            for s in LISTING_STATUSES
        }
        self._require()
        listed = self.client.search_count(
            self.PRODUCT_MODEL, [["ebay_listed", "=", True]]
        )
        candidates = self.client.search_count(
            self.PRODUCT_MODEL,
            [["ebay_listed", "=", True], ["ebay_suggested_discount_pct", ">", 0]],
        )
        never_researched = self.client.search_count(
            self.PRODUCT_MODEL,
            [["ebay_listed", "=", True], ["ebay_comp_fetched_at", "=", False]],
        )
        return {
            "summary": (
                f"eBay: {by_status['active']} active listings, "
                f"{by_status['draft']} draft, {listed} products listed. "
                f"{candidates} repricing candidates, "
                f"{never_researched} never researched"
            ),
            "listings_by_status": by_status,
            "products_listed": listed,
            "repricing_candidates": candidates,
            "never_researched": never_researched,
        }
