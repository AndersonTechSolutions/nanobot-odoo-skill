"""
Facebook Marketplace operations for the ``fb_marketplace_lister`` module
(Odoo 17 ``fb.marketplace.listing``).

The module tracks a product's life on Facebook Marketplace, where listings
expire rather than persist: a listing goes ``draft -> listed``, becomes
``renewal_due`` after Facebook's renewal window, and ends at ``sold`` or
``ended``. The renewal queue is the point of the module — an unrenewed
listing quietly stops being shown.

Four things shape this class:

* **Access is gated.** ``fb.marketplace.listing`` carries ACLs for
  ``fb_marketplace_lister.group_fb_marketplace_user`` (read/write/create) and
  ``...group_fb_marketplace_manager`` (adds unlink). The API user must be in
  one of them or *every* call raises an access fault — there is no partial
  read. The class declares them in :data:`FB_GROUPS`,
  so the inherited :meth:`BaseOps.access_check` reports the missing group by
  name instead of letting the fault surface raw.

* **Price is not writable here.** ``price`` is
  ``related="product_tmpl_id.list_price"`` and readonly, so a listing's price
  is the product's price. :meth:`set_price` writes through to the product
  template rather than pretending the listing owns the value. ``price`` *is*
  searchable (Odoo rewrites the domain onto the stored target), so filters on
  it stay server-side.

* **Going live needs the Marketplace URL.** ``action_mark_listed`` raises
  without ``listing_url`` — it is the only handle on the real Facebook post,
  so a listing marked live without one cannot be found again.
  :meth:`FbMarketplaceOps.mark_listed` takes the URL and writes it first.

* **``days_listed`` is** ``searchable: False`` — a domain on it is silently
  dropped and returns the unfiltered set. :meth:`stale_listings` therefore
  filters client-side via :meth:`BaseOps.search_computed`. ``renewal_date``,
  ``days_to_sell`` and ``suggested_price`` *are* stored and searchable, so
  those filters run server-side.
"""

import base64
import binascii
import html
import logging
import math
import os
import stat
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from ..errors import (
    OdooAccessError, OdooAuthenticationError, OdooConnectionError, OdooError,
    server_lacks_method,
)
from ._base import BaseOps, utc_stamp

logger = logging.getLogger("odoo_skill")

#: Package weight (lb, core ``weight``) and box dimensions (inches, sale_ebay
#: 1.40 ``ebay_pkg_*_in``) plus the last scale read (fb_marketplace_lister
#: 4.2 ``weight_measured_on``). Related onto the listing from its product;
#: dropped by ``_existing()`` on databases without those module versions.
_PACKAGE_FIELDS = [
    "weight", "ebay_pkg_length_in", "ebay_pkg_width_in", "ebay_pkg_height_in",
    "weight_measured_on",
    # 4.3 warehouse box (stock.package.type) whose size fills the dims.
    "ebay_package_type_id",
]

_LIST_FIELDS = [
    "id", "name", "product_tmpl_id", "state", "condition", "price",
    "suggested_price", "listed_date", "renewal_date", "listing_url",
    *_PACKAGE_FIELDS,
    # fb_marketplace_lister 4.9 pending state: the Facebook-side action the
    # lister still owes (dropped by _existing() on an older module).
    "fb_sync_action", "fb_sync_error", "fb_sync_token",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "description", "first_listed_date", "sold_date", "days_listed",
    "days_to_sell", "ai_generated", "is_temp", "image_ids", "currency_id",
    "create_date", "write_uid", "location_id", "shippable",
    # fb_marketplace_lister 4.x per-sale history; dropped by _existing()
    # on databases still running an older module.
    "sold_price", "sold_qty", "sale_count", "can_record_sale",
    # fb_marketplace_lister 4.12 description template: ``description`` is the
    # body only; ``description_full`` (body + pickup / shipping / payment /
    # credit-card footer) is what goes on Facebook. Dropped by _existing()
    # on an older module, so posters fall back to ``description``.
    "description_full", "description_footer", "accept_cards",
    # fb_marketplace_lister 4.13: the Facebook category picked in Odoo. Dropped
    # by _existing() on an older module, so posters fall back to the keyword
    # proposal in the lister skill.
    "category_id",
]

#: ``product.template`` model the package RPCs live on; the listing's own
#: ``fb_read_scale`` / ``fb_set_package`` delegate to it.
_PRODUCT_MODEL = "product.template"

#: ``box`` spellings that clear the warehouse box (sent as ``False``).
_BOX_CLEAR = {"", "none", "clear", "false", "0"}


def _box_arg(box: Any) -> Any:
    """``box`` as the server wants it: an int id, a text (name / size), or
    ``False`` to clear. ``None`` is the caller's "not given" and never gets
    here. Non-text/non-int values are refused before any RPC."""
    if box is False:
        return False
    if isinstance(box, bool) or isinstance(box, float) and not box.is_integer():
        raise ValueError(f"box must be an id, a name or a size like '11x8x4', got {box!r}")
    if isinstance(box, (int, float)):
        return int(box) or False
    if isinstance(box, str):
        text = box.strip()
        return False if text.lower() in _BOX_CLEAR else text
    raise ValueError(f"box must be an id, a name or a size like '11x8x4', got {box!r}")

#: ``product.template`` fields read by :meth:`FbMarketplaceOps.create_from_product`.
_PRODUCT_FIELDS = [
    "id", "name", "default_code", "list_price", "type", "qty_available",
    "description_sale", "fb_temp", "fb_listed",
]

#: sale_ebay fields for the eBay side of a product; read separately because
#: the eBay connector is optional and a missing field fails the whole read.
_EBAY_FIELDS = ["ebay_listed", "ebay_listing_status", "ebay_fixed_price", "ebay_url"]

#: ``product.template`` fields in the channel-gap lists.
_GAP_FIELDS = [
    "id", "name", "default_code", "list_price", "qty_available", "fb_temp",
    "fb_channel_status", "ebay_channel_status",
]

#: ``state`` values, in lifecycle order.
STATES = ["draft", "listed", "renewal_due", "pending", "sold", "ended"]

#: States a listing is still working in — not yet sold or withdrawn.
#: ``pending`` (module 4.9: buyer lined up) is still open — the post is up and
#: a second draft for the same product would be a duplicate.
OPEN_STATES = ["draft", "listed", "renewal_due", "pending"]


def _strict_bool(value: Any, name: str) -> bool:
    """A real boolean only. ``"false"`` (a string from a JSON/CLI caller) is
    truthy under ``bool()`` and would queue a Facebook action or acknowledge
    a failed sync as done, so anything but True/False is rejected."""
    if isinstance(value, bool):
        return value
    raise OdooError(f"{name} must be true or false (got {value!r})")

#: ``condition`` values accepted by the module.
CONDITIONS = ["new", "refurbished", "like_new", "good", "fair", "for_parts"]

#: The groups that can reach the model at all.
FB_GROUPS = (
    "fb_marketplace_lister.group_fb_marketplace_user",
    "fb_marketplace_lister.group_fb_marketplace_manager",
)


class FbMarketplaceOps(BaseOps):
    """Workflow operations on ``fb.marketplace.listing``."""

    MODEL = "fb.marketplace.listing"
    MODULE = "fb_marketplace_lister"
    IMAGE_MODEL = "fb.marketplace.listing.image"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "renewal_date asc, id desc"
    REQUIRED_GROUPS = FB_GROUPS

    ALLOWED_ACTIONS = frozenset({
        # lifecycle
        "action_mark_listed",
        "action_mark_sold",
        "action_renewed",
        "action_end_listing",
        "action_reset_draft",
        # content / pricing
        "action_generate_ai_content",
        "action_apply_suggested_price",
        # media / print
        "action_add_product_image",
        "action_print_fb_label",
        # sales (4.x): plain-dict RPCs, gated server-side by the
        # "Record Sales" group for the invoice path
        "fb_record_sale",
        "fb_invoice_sales",
        # package (4.2): read the product's weight from a Ventor scale
        "action_read_scale",
        # pending (4.9): Odoo state flips + the agent's report-back
        "action_mark_pending",
        "action_mark_available",
        "fb_sync_done",
        # checkout orders (4.16): import a Facebook order onto this listing
        "fb_import_order",
    })

    #: Facebook-side actions ``sync_queue`` can carry (module 4.9).
    SYNC_ACTIONS = ("mark_pending", "mark_available")

    # ── Reads ────────────────────────────────────────────────────────

    def active_listings(self, limit: int = 50) -> list[dict]:
        """Listings currently live on Marketplace."""
        return self.search([["state", "=", "listed"]], limit=limit)

    def pending_listings(self, limit: int = 50) -> list[dict]:
        """Listings with a buyer lined up (module 4.9 ``pending`` state); the
        post is still on Facebook, shown as Pending."""
        return self.search([["state", "=", "pending"]], limit=limit)

    def sync_queue(self, limit: int = 50) -> list[dict]:
        """Facebook-side actions Odoo is waiting on, oldest request first.

        Each row carries ``fb_sync_action`` (``mark_pending`` /
        ``mark_available``), the Marketplace ``listing_url`` to act on, and
        ``fb_sync_error`` from the last failed attempt (``False`` when none).
        Rows with an error are still queued: report them, do not retry
        blindly. ``fb_sync_done`` closes a row. Raises on a module older
        than 4.9 (no queue to read).
        """
        self._require()
        try:
            rows = self.client.execute(self.MODEL, "fb_sync_queue", limit=int(limit))
        except OdooError as exc:
            if server_lacks_method(exc, "fb_sync_queue"):
                raise OdooError("The Facebook sync queue needs fb_marketplace_lister "
                                ">= 4.9 on this server.") from exc
            raise
        return rows if isinstance(rows, list) else []

    def draft_listings(self, limit: int = 50) -> list[dict]:
        """Listings prepared but not yet posted to Marketplace."""
        return self.search([["state", "=", "draft"]], limit=limit)

    def renewal_due(self, within_days: int = 0, limit: int = 50) -> list[dict]:
        """Listings that need renewing, soonest first.

        Args:
            within_days: Also include listings whose ``renewal_date`` falls
                inside the next *N* days. ``0`` (default) returns only what is
                already due.

        ``renewal_date`` is stored and searchable, so this is a server-side
        domain and exact — no scan cap applies.
        """
        return self.search(
            self._renewal_domain(within_days),
            limit=limit, order="renewal_date asc",
        )

    def _renewal_domain(self, within_days: int = 0) -> list:
        """Domain for the renewal queue — shared by the list and the count.

        Kept as one definition so :meth:`marketplace_summary` counts exactly
        what :meth:`renewal_due` lists. It is fully server-side, so the count
        is exact rather than a page length.
        """
        return [
            "&",
            ["state", "in", ["listed", "renewal_due"]],
            "|",
            ["state", "=", "renewal_due"],
            "&",
            ["renewal_date", "!=", False],
            ["renewal_date", "<=", utc_stamp(timedelta(days=max(within_days, 0)))],
        ]

    def stale_listings(self, older_than_days: int = 30, limit: int = 50) -> list[dict]:
        """Live listings that have been up a long time without selling.

        Filtered client-side: ``days_listed`` is computed with
        ``searchable: False``, so a domain on it would be dropped and return
        every live listing instead. The scan is bounded — see
        :meth:`BaseOps.search_computed`.
        """
        return self.search_computed(
            [["state", "in", ["listed", "renewal_due"]]],
            lambda r: (r.get("days_listed") or 0) >= older_than_days,
            limit=limit, extra_fields=["days_listed"],
        )

    def listings_for_product(self, product_tmpl_id: int, limit: int = 20) -> list[dict]:
        """Every listing ever raised for a product template."""
        return self.search(
            [["product_tmpl_id", "=", product_tmpl_id]], limit=limit, order="id desc"
        )

    def find_listing(self, query: str, limit: int = 10) -> list[dict]:
        """Locate listings by title or by the product they point at."""
        return self.search(
            ["|",
             ["name", "ilike", query],
             ["product_tmpl_id.name", "ilike", query]],
            limit=limit,
        )

    def sold_listings(self, since_days: int = 30, limit: int = 50) -> list[dict]:
        """Recently sold listings, newest first."""
        return self.search(
            [["state", "=", "sold"],
             ["sold_date", ">=", utc_stamp(-timedelta(days=since_days))]],
            limit=limit, order="sold_date desc",
        )

    def repricing_candidates(self, limit: int = 50) -> list[dict]:
        """Open listings whose AI suggested price differs from the live price.

        Both operands are searchable, but comparing two fields to each other
        is not expressible in an Odoo domain, so the difference is evaluated
        client-side over the open set.
        """
        return self.search_computed(
            [["state", "in", OPEN_STATES], ["suggested_price", ">", 0]],
            lambda r: _differs(r.get("suggested_price"), r.get("price")),
            limit=limit, extra_fields=["suggested_price", "price"],
        )

    def needs_content(self, limit: int = 50) -> list[dict]:
        """Draft listings with no description yet — the AI-content queue."""
        return self.search(
            [["state", "=", "draft"], ["description", "in", [False, ""]]],
            limit=limit,
        )

    def resolve_product(self, ref: str, limit: int = 10) -> dict:
        """Turn an operator-typed reference into a ``product.template`` id.

        The ``fb <ref>`` direction (catalog / eBay → FB): a bare integer is a
        **product.template id** (unlike ``ebay.resolve_item``, where it is an
        FB listing id); ``sku XYZ`` / an exact ``default_code`` match next;
        anything else is a case-insensitive name search returning
        ``candidates``, with ``product_tmpl_id`` set only on a unique hit.
        """
        self._require()
        text = str(ref or "").strip()
        out: dict[str, Any] = {"ref": text, "kind": "none", "product_tmpl_id": None,
                               "candidates": [], "summary": ""}
        if not text:
            out["summary"] = "Empty reference."
            return out
        m = re.fullmatch(r"(?:product[:\s]*|#)?(\d+)", text, re.IGNORECASE)
        if m:
            rows = self.client.search_read(
                "product.template", [["id", "=", int(m.group(1))]],
                fields=_GAP_FIELDS, limit=1)
            if rows:
                out.update(kind="id", product_tmpl_id=rows[0]["id"], candidates=rows,
                           summary=f"Product #{rows[0]['id']} {rows[0]['name']}")
            else:
                out["summary"] = f"No product with id {m.group(1)}."
            return out
        sku = re.sub(r"^sku\s*:?\s*", "", text, flags=re.IGNORECASE).strip()
        rows = self.client.search_read(
            "product.template", [["default_code", "=ilike", sku]],
            fields=_GAP_FIELDS, limit=2)
        if len(rows) == 1:
            out.update(kind="sku", product_tmpl_id=rows[0]["id"], candidates=rows,
                       summary=f"Product #{rows[0]['id']} {rows[0]['name']} (SKU {sku})")
            return out
        rows = self.client.search_read(
            "product.template",
            ["|", ["name", "ilike", text], ["default_code", "ilike", text]],
            fields=_GAP_FIELDS, limit=max(int(limit), 2), order="name")
        out["candidates"] = rows[:max(int(limit), 1)]
        if len(rows) == 1:
            out.update(kind="name", product_tmpl_id=rows[0]["id"],
                       summary=f"Product #{rows[0]['id']} {rows[0]['name']}")
        elif rows:
            out.update(kind="ambiguous",
                       summary=f"{len(rows)} products match {text!r}; pick one by id.")
        else:
            out["summary"] = f"No product matches {text!r}."
        return out

    def ebay_live_not_on_fb(self, limit: int = 50) -> list[dict]:
        """Products live on eBay with no open FB listing (multichannel gap).

        ``ebay_listed`` is sale_ebay's stored flag (Active / Out Of Stock);
        ``fb_listed`` is this module's stored flag (draft / listed /
        renewal_due). Both stored, so the domain is server-side and exact.
        Raises the usual field error when sale_ebay is not installed.
        """
        self._require()
        return self.client.search_read(
            "product.template",
            [["ebay_listed", "=", True], ["fb_listed", "=", False]],
            fields=_GAP_FIELDS, limit=limit, order="name",
        )

    def fb_not_on_ebay(self, limit: int = 50, include_temp: bool = True) -> list[dict]:
        """Products with an open FB listing that are not live on eBay.

        Temp items (``fb_temp``) are one-off Marketplace products; they are
        included by default so the digest can flag them, and each row carries
        ``fb_temp`` so the caller can say so.
        """
        self._require()
        domain = [["fb_listed", "=", True], ["ebay_listed", "=", False]]
        if not include_temp:
            domain.append(["fb_temp", "=", False])
        return self.client.search_read(
            "product.template", domain,
            fields=_GAP_FIELDS, limit=limit, order="name",
        )

    def channel_gaps(self, limit: int = 50) -> dict:
        """Both gap lists plus counts — the Monday digest payload."""
        ebay_only = self.ebay_live_not_on_fb(limit=limit)
        fb_only = self.fb_not_on_ebay(limit=limit)
        ebay_total = self.client.search_count(
            "product.template", [["ebay_listed", "=", True], ["fb_listed", "=", False]])
        fb_total = self.client.search_count(
            "product.template", [["fb_listed", "=", True], ["ebay_listed", "=", False]])
        temp = self.client.search_count(
            "product.template",
            [["fb_listed", "=", True], ["ebay_listed", "=", False], ["fb_temp", "=", True]])
        truncated = ebay_total > len(ebay_only) or fb_total > len(fb_only)
        return {
            "summary": (
                f"{ebay_total} live on eBay but not on FB; "
                f"{fb_total} on FB but not on eBay ({temp} temp item(s))"
                + (f"; showing the first {limit} of each" if truncated else "")
            ),
            "ebay_live_not_on_fb_count": ebay_total,
            "fb_not_on_ebay_count": fb_total,
            "fb_temp_count": temp,
            "truncated": truncated,
            "ebay_live_not_on_fb": ebay_only,
            "fb_not_on_ebay": fb_only,
        }

    def get_images(self, listing_id: int) -> list[dict]:
        """Photo rows attached to a listing, in display order.

        Image *data* is deliberately not returned — a base64 ``image`` field
        would blow up a chat transcript for no benefit. Captions and ids are
        enough to reason about, and to target a delete. When the bytes are
        actually needed — to re-upload the photo somewhere off Odoo — call
        :meth:`get_image_data` instead, which opts into the binary explicitly.
        """
        self._require()
        return self.client.search_read(
            self.IMAGE_MODEL,
            [["listing_id", "=", listing_id]],
            fields=["id", "name", "sequence"],
            order="sequence, id",
        )

    def get_image_data(self, listing_id: int, limit: int = 50) -> list[dict]:
        """Photo rows **with** the base64 ``image`` binary, in display order.

        The one read that returns the actual bytes. :meth:`get_images` withholds
        them on purpose (transcript bloat); an external poster — the FB
        Marketplace lister — needs the real payload to hand to a file upload, so
        this is the explicit opt-in.

        The binary field is ``image`` (base64, no ``data:`` prefix), the same
        field :meth:`add_image` writes. ``limit`` is passed explicitly rather
        than leaning on ``search_read``'s implicit page size — a listing never
        holds that many photos, but a binary read stays bounded on purpose.

        Callers should treat the returned ``image`` values as opaque bytes: do
        not echo them into a transcript or log. ``image`` may be ``False`` on a
        row saved without a payload; skip those.
        """
        self._require()
        return self.client.search_read(
            self.IMAGE_MODEL,
            [["listing_id", "=", listing_id]],
            fields=["id", "name", "sequence", "image"],
            order="sequence, id",
            limit=limit,
        )

    # ── Writes ───────────────────────────────────────────────────────

    def create_listing(
        self,
        product_tmpl_id: int,
        name: Optional[str] = None,
        condition: str = "refurbished",
        description: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Draft a Marketplace listing for a product.

        Args:
            product_tmpl_id: ``product.template`` to list. Required by the model.
            name: Listing title; defaults to the product's own name.
            condition: One of :data:`CONDITIONS`.
            description: Listing body. Leave empty and use
                :meth:`generate_content` to have the module draft it.
            **extra: Any other ``fb.marketplace.listing`` field.

        Returns:
            The created listing in detail form. It starts in ``draft`` —
            posting to Marketplace is an explicit :meth:`mark_listed` call.
        """
        if condition not in CONDITIONS:
            raise ValueError(
                f"condition must be one of {CONDITIONS}, got {condition!r}"
            )

        title = name
        if not title:
            rows = self.client.read(
                "product.template", [product_tmpl_id], fields=["name"]
            )
            if not rows:
                raise ValueError(f"No product.template with id {product_tmpl_id}")
            title = rows[0]["name"]

        values: dict[str, Any] = {
            "product_tmpl_id": product_tmpl_id,
            "name": title,
            "condition": condition,
        }
        if description:
            values["description"] = description
        values.update(extra)

        record = self.create(values)
        return {
            "summary": (
                f"Draft listing '{record['name']}' created ({condition}). "
                "Not yet posted — call mark_listed when it is live on Facebook."
            ),
            "listing": record,
        }

    def create_from_product(
        self,
        product_tmpl_id: int,
        generate: bool = True,
        condition: str = "refurbished",
        location_id: Optional[int] = None,
        shippable: Optional[bool] = None,
        accept_cards: Optional[bool] = None,
    ) -> dict:
        """Draft an FB listing for a catalog / eBay-live product (``fb <ref>``).

        Refuses when the product already has an open listing (returns it
        instead, ``created: False``) so ``fb <ref>`` twice never doubles up.
        Seeds the description from ``description_sale`` and, with
        ``generate``, runs the module's AI copy over it. The result carries
        what the operator needs to review the draft: on-hand quantity and,
        when sale_ebay is installed, the eBay price so a gap against the FB
        price (``list_price``, Q8) can be flagged.
        """
        self._require()
        rows = self.client.read(
            "product.template", [product_tmpl_id], fields=_PRODUCT_FIELDS)
        if not rows:
            raise ValueError(f"No product.template with id {product_tmpl_id}")
        tmpl = rows[0]
        ebay: dict[str, Any] = {}
        try:
            erows = self.client.read(
                "product.template", [product_tmpl_id], fields=_EBAY_FIELDS)
            ebay = erows[0] if erows else {}
        except OdooError as exc:
            # Only "no such field" means sale_ebay is absent; anything else
            # (access, auth, connection, a real server fault) must surface.
            if isinstance(exc, (OdooConnectionError, OdooAuthenticationError,
                                OdooAccessError)) or not re.search(
                    r"invalid field|field .* does not exist|unknown field",
                    str(exc), re.IGNORECASE):
                raise
            ebay = {}

        existing = self.search(
            [["product_tmpl_id", "=", product_tmpl_id], ["state", "in", OPEN_STATES]],
            limit=1, order="id desc")
        if existing:
            listing = self.get(existing[0]["id"])
            return {
                "summary": (
                    f"'{tmpl['name']}' already has open FB listing "
                    f"#{listing['id']} ({listing.get('state')}); nothing created."
                ),
                "created": False,
                "listing": listing,
                **self._channel_facts(tmpl, ebay),
            }

        extra: dict[str, Any] = {}
        if location_id:
            extra["location_id"] = int(location_id)
        if shippable is not None:
            extra["shippable"] = bool(shippable)
        if accept_cards is not None:
            # 4.12 description-template flag; unset = the Settings default.
            extra["accept_cards"] = bool(accept_cards)
        created = self.create_listing(
            product_tmpl_id, condition=condition,
            description=_plain_text(tmpl.get("description_sale")) or None,
            **extra)
        listing = created["listing"]
        notes: list[str] = []
        # The existence check and the create are separate RPCs; a retried
        # create whose first response was lost, or a concurrent call, can
        # leave two open listings. Say so rather than pretend it can't happen.
        siblings = self.search(
            [["product_tmpl_id", "=", product_tmpl_id], ["state", "in", OPEN_STATES],
             ["id", "!=", listing["id"]]], limit=5, order="id")
        if siblings:
            ids = ", ".join(f"#{r['id']}" for r in siblings)
            notes.append(
                f"DUPLICATE: product already has open FB listing(s) {ids}; "
                f"close the extra one before posting.")
        generated = False
        if generate:
            try:
                gen = self.generate_content(listing["id"])
                listing = gen["listing"]
                generated = True
            except OdooError as exc:
                notes.append(f"AI copy not generated: {exc}")
        facts = self._channel_facts(tmpl, ebay)
        if facts["ebay_price_gap"] is not None:
            notes.append(
                f"eBay price {facts['ebay_price']} vs FB price {facts['fb_price']} "
                f"(gap {facts['ebay_price_gap']:+.2f})")
        if not facts["on_hand"]:
            notes.append("No stock on hand.")
        return {
            "summary": (
                f"Draft FB listing #{listing['id']} created for '{tmpl['name']}'"
                + (" with AI copy" if generated else "")
                + ". Review, then post it to Facebook."
            ),
            "created": True,
            "ai_generated": generated,
            "listing": listing,
            "notes": notes,
            **facts,
        }

    @staticmethod
    def _channel_facts(tmpl: dict, ebay: dict) -> dict:
        """On-hand, prices and the eBay price gap for a draft review."""
        fb_price = tmpl.get("list_price") or 0.0
        ebay_price = ebay.get("ebay_fixed_price") or None
        live = bool(ebay.get("ebay_listed"))
        gap = None
        if live and ebay_price and _differs(ebay_price, fb_price):
            gap = round(float(ebay_price) - float(fb_price), 2)
        return {
            "product": {
                "id": tmpl.get("id"), "name": tmpl.get("name"),
                "default_code": tmpl.get("default_code") or "",
                "fb_temp": bool(tmpl.get("fb_temp")),
            },
            "on_hand": tmpl.get("qty_available") or 0.0,
            "fb_price": fb_price,
            "ebay_live": live,
            "ebay_status": ebay.get("ebay_listing_status") or "",
            "ebay_price": ebay_price,
            "ebay_url": ebay.get("ebay_url") or "",
            "ebay_price_gap": gap,
        }

    def set_price(self, listing_id: int, price: float) -> dict:
        """Set a listing's price by writing the product template's list price.

        ``fb.marketplace.listing.price`` is ``related`` and readonly, so
        writing it raises. The value genuinely lives on the product, and
        changing it there is what the UI does too — but it also moves the
        price everywhere else that product is sold, which the summary says
        out loud.
        """
        record = self.get(listing_id)
        tmpl = record.get("product_tmpl_id")
        if not tmpl:
            raise ValueError(f"Listing {listing_id} has no product template")
        tmpl_id = tmpl[0] if isinstance(tmpl, (list, tuple)) else tmpl
        self.client.write("product.template", tmpl_id, {"list_price": price})
        return {
            "summary": (
                f"Price for '{record['name']}' set to {price} via product "
                f"template #{tmpl_id}. This is the product's list price — it "
                "applies to every channel selling it, not just Marketplace."
            ),
            "listing": self.get(listing_id),
        }

    def mark_listed(
        self, listing_id: int, listing_url: Optional[str] = None
    ) -> dict:
        """Record that the listing is now live on Facebook Marketplace.

        The module refuses to mark a listing live without its Marketplace URL
        ("Paste the Facebook Marketplace URL before marking as listed") — the
        URL is the only handle anyone has on the real post, so a listing
        marked live without one is untraceable. Pass it here and it is written
        before the action fires, the same write-then-act shape as
        ``RepairOps.post_customer_update``.

        Args:
            listing_id: Listing to mark live.
            listing_url: The Facebook Marketplace post URL. Required unless
                the listing already carries one.
        """
        if listing_url:
            self.update(listing_id, {"listing_url": listing_url})
        else:
            current = self.get(listing_id)
            if not current.get("listing_url"):
                raise ValueError(
                    "listing_url is required to mark this listing live — the "
                    "module rejects a listed record with no Marketplace URL. "
                    "Pass the Facebook post URL as listing_url."
                )
        return self.run_action(listing_id, "action_mark_listed")

    def mark_sold(
        self,
        listing_id: int,
        qty: float = 1.0,
        price: Optional[float] = None,
        invoice: bool = False,
        close: Optional[bool] = None,
        ref: Optional[str] = None,
        b2b: bool = False,
    ) -> dict:
        """Record a Facebook sale on a listing (``fb_record_sale``).

        Moves ``qty`` units out of stock at ``price`` each (``None`` = list
        price, ``0`` = giveaway). Marketplace buyers pay no sales tax, so the
        invoice carries ``price`` only; ``b2b=True`` marks a business sale
        whose product taxes are charged on top. ``invoice=True``
        also raises a paid sales order — the server requires the "Record
        Sales" group for that. ``close`` forces the listing closed / kept
        live; ``None`` lets the module decide (temp items always close, a
        catalog product closes at zero stock). ``ref`` is an idempotency key:
        the same ref twice returns ``duplicate: True`` and moves nothing.
        The plain ``action_mark_sold`` button is no longer used — it closes
        the listing without a sale row.
        """
        kwargs: dict[str, Any] = {"qty": float(qty), "invoice": bool(invoice)}
        if price is not None:
            kwargs["price"] = float(price)
        if close is not None:
            kwargs["close"] = bool(close)
        # Always send a ref: the transport retries lost responses, and only
        # a stable ref makes the second attempt a no-op on the server.
        kwargs["ref"] = str(ref) if ref else f"auto-{uuid.uuid4().hex[:16]}"
        if b2b:
            kwargs["b2b"] = True
        result = self.run_action(listing_id, "fb_record_sale", **kwargs)
        sale = result["returned"] if isinstance(result["returned"], dict) else {}
        record = result["record"]
        if sale.get("duplicate"):
            summary = f"Ref {ref!r} already recorded on listing #{listing_id}; nothing moved."
        else:
            summary = (
                f"Sale recorded on '{record.get('name')}': {sale.get('qty', qty)} × "
                f"{sale.get('price', price if price is not None else record.get('price'))}"
                + (" (B2B, taxed)" if sale.get("b2b") else "")
                + (f", order {sale.get('sale_order')}" if sale.get("sale_order") else "")
                + ("; listing closed" if sale.get("closed") else
                   f"; {sale.get('remaining', '?')} left, listing stays live")
            )
            if sale.get("closed"):
                summary += self._archive_note(record)
        return {"summary": summary, "sale": sale, "listing": record}

    def _archive_note(self, record: dict) -> str:
        """Say whether the closed listing's product got archived. The module
        archives a temp product when its listing closes on a sale; a catalog
        product stays active on purpose. Read by id (not search) so an
        archived row still comes back; a read failure must not undo a sale
        that already happened, so it degrades to a note."""
        tmpl = record.get("product_tmpl_id")
        tmpl_id = tmpl[0] if isinstance(tmpl, (list, tuple)) else tmpl
        if not tmpl_id:
            return ""
        try:
            rows = self.client.read("product.template", int(tmpl_id), ["active"])
        except Exception as exc:  # noqa: BLE001 - surface, never raise after the sale
            return f"; product #{tmpl_id} archive status unknown ({exc})"
        if not rows:
            return f"; product #{tmpl_id} archive status unknown"
        if rows[0].get("active"):
            return (f"; product #{tmpl_id} still active"
                    + ("" if record.get("is_temp") else " (catalog product, kept)"))
        return f"; product #{tmpl_id} archived"

    def record_sale(self, listing_id: int) -> dict:
        """After the fact: invoice every cash sale on a listing that has no
        order yet (``fb_invoice_sales``, one paid order per sale at its own
        price, no delivery). Needs the "Record Sales" group."""
        result = self.run_action(listing_id, "fb_invoice_sales")
        out = result["returned"] if isinstance(result["returned"], dict) else {}
        orders = out.get("sale_orders") or []
        return {
            "summary": (
                f"{len(out.get('sale_ids') or [])} sale(s) on listing #{listing_id} "
                f"invoiced: {', '.join(orders) or '-'}"
            ),
            "result": out,
            "listing": result["record"],
        }

    # ── Facebook checkout orders (fb_marketplace_lister 4.16) ────────
    #
    # A checkout order is one the buyer paid for on Facebook and we ship.
    # The browser half reads Marketplace > Orders; these calls carry what it
    # saw. All of them need the "Record Sales" group server-side, and the
    # server re-validates every field (digits-only order number, finite
    # money, fb|own label mode).

    _SALE_MODEL = "fb.marketplace.sale"
    _ORDER_KEYS = ("fb_order_id", "qty", "unit_price", "label_mode",
                   "buyer_shipping", "buyer_name", "ship_to", "ship_by")
    _SHIP_TO_KEYS = ("street", "street2", "city", "state", "zip")
    _MAX_LABEL_BYTES = 5 * 1024 * 1024
    #: Keys of a module order result that reach the agent (no blobs).
    _RESULT_KEYS = (
        "success", "duplicate", "imported", "accessible", "fb_order_id",
        "sale_id", "listing_id", "listing_state", "sale_order_id", "sale_order",
        "partner", "amount", "label_mode", "delivery", "delivery_state",
        "tracking", "invoices", "invoice_paid", "street_known", "fund_status",
        "fee", "payout_amount", "actions", "attachment_id", "label_attachment_id", "print_queued",
        "printer", "carrier", "print_error",
    )

    @staticmethod
    def _order_no(value: Any) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{6,20}", text):
            raise OdooError("fb_order_id must be the 6-20 digit Facebook order number")
        return text

    @classmethod
    def _order_view(cls, out: Any) -> dict:
        """Allowlisted, size-bounded view of a module order result."""
        if not isinstance(out, dict):
            raise OdooError(f"unexpected server reply ({type(out).__name__})")
        view = {}
        for key in cls._RESULT_KEYS:
            if key not in out:
                continue
            value = out[key]
            if isinstance(value, str):
                value = value[:200]
            elif isinstance(value, list):
                value = [str(v)[:64] for v in value[:20]]
            elif not isinstance(value, (bool, int, float)) and value is not None:
                value = str(value)[:200]
            view[key] = value
        return view

    def _ship_to(self, value: Any) -> dict:
        if not value:
            return {}
        if not isinstance(value, dict):
            raise OdooError("ship_to must be an object")
        return {k: str(value[k])[:128] for k in self._SHIP_TO_KEYS if value.get(k)}

    @staticmethod
    def _label_dir() -> Path:
        """The one directory label PDFs are read from / written to:
        ``FB_LISTER_LABEL_DIR``, else ``$FB_LISTER_EXPORT_DIR/orders/labels``."""
        raw = os.environ.get("FB_LISTER_LABEL_DIR") or os.path.join(
            os.environ.get("FB_LISTER_EXPORT_DIR") or "", "orders", "labels")
        if not os.path.isabs(raw):
            raise OdooError("set FB_LISTER_LABEL_DIR (or FB_LISTER_EXPORT_DIR) to an absolute path")
        path = Path(raw)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        real = Path(os.path.realpath(path))
        if real != path.absolute() or not real.is_dir():
            raise OdooError(f"label dir must be a real directory, not a link: {raw}")
        return real

    def _open_label_dir(self) -> tuple[Path, int]:
        """Open :meth:`_label_dir` by walking it component by component from
        ``/`` with no-follow, descriptor-relative opens, then do every file
        operation relative to the final descriptor (``dir_fd``): no link in
        the path, now or swapped in later, can redirect the open."""
        root = self._label_dir()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        dfd = os.open("/", flags)
        try:
            for part in root.parts[1:]:
                nxt = os.open(part, flags, dir_fd=dfd)
                os.close(dfd)
                dfd = nxt
        except OSError as exc:
            os.close(dfd)
            raise OdooError(f"cannot open label dir {root}: {exc.strerror}") from None
        return root, dfd

    def _read_label(self, pdf_path: str) -> bytes:
        """Open a label directly inside :meth:`_label_dir` without following
        links and read at most the size cap from the opened descriptor."""
        root, dfd = self._open_label_dir()
        try:
            name = os.path.basename(pdf_path)
            if os.path.abspath(pdf_path) != str(root / name) or name in ("", ".", ".."):
                raise OdooError(f"label must be a file directly inside {root}")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except OSError as exc:
                raise OdooError(f"cannot open label: {exc.strerror}") from None
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise OdooError("label is not a regular file")
                blob = fh.read(self._MAX_LABEL_BYTES + 1)
        finally:
            os.close(dfd)
        if not blob.startswith(b"%PDF-") or len(blob) > self._MAX_LABEL_BYTES:
            raise OdooError("the label must be a PDF of at most 5 MB")
        return blob

    @staticmethod
    def _ident(value: Any, name: str, limit: int) -> str:
        """A short free-text identifier (tracking, carrier, payout id)."""
        if not isinstance(value, str) or not value.strip():
            raise OdooError(f"{name} must be non-empty text")
        value = value.strip()
        if len(value) > limit:
            raise OdooError(f"{name} is longer than {limit} characters")
        return value

    @staticmethod
    def _positive_id(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise OdooError(f"{name} must be a positive integer")
        text = str(value).strip()
        if not text.isdigit() or int(text) <= 0:
            raise OdooError(f"{name} must be a positive integer")
        return int(text)

    def orders_status(self, fb_order_ids: list) -> dict:
        """Read-only: where each Facebook order stands in Odoo (imported?,
        sales order, delivery state, tracking, invoice paid?)."""
        self._require()
        ids = [self._order_no(i) for i in (fb_order_ids or [])]
        if len(ids) > 500:
            raise OdooError("at most 500 order ids per call")
        if not ids:
            return {"success": True, "orders": []}
        out = self.client.execute(self._SALE_MODEL, "fb_orders_status", ids)
        rows = out.get("orders") if isinstance(out, dict) else None
        if not isinstance(rows, list):
            raise OdooError("unexpected server reply")
        return {"success": True, "orders": [self._order_view(r) for r in rows]}

    def import_order(self, listing_id: int, order: dict) -> dict:
        """Import one Facebook checkout order onto a listing
        (``fb_import_order``): buyer contact, untaxed sales order, delivery
        left open, invoice posted and unpaid until Facebook pays out.
        Importing the same order again returns ``duplicate: True``."""
        listing_id = self._positive_id(listing_id, "listing_id")
        if not isinstance(order, dict):
            raise OdooError("order must be an object")
        payload = {k: order[k] for k in self._ORDER_KEYS if k in order}
        payload["fb_order_id"] = self._order_no(order.get("fb_order_id"))
        if payload.get("label_mode") not in ("fb", "own"):
            raise OdooError('label_mode must be "fb" or "own"')
        for key in ("qty", "unit_price", "buyer_shipping"):
            if key in payload:
                value = payload[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not math.isfinite(value) or value < 0:
                    raise OdooError(f"{key} must be a finite number of 0 or more")
        if "unit_price" not in payload:
            raise OdooError("unit_price is required")
        if "ship_to" in payload:
            payload["ship_to"] = self._ship_to(payload["ship_to"])
        result = self.run_action(listing_id, "fb_import_order", order=payload)
        out = self._order_view(result["returned"])
        if out.get("duplicate"):
            summary = (f"Facebook order {payload['fb_order_id']} was already imported "
                       f"({out.get('sale_order') or '?'}); nothing changed.")
        else:
            summary = (f"Facebook order {payload['fb_order_id']} imported as "
                       f"{out.get('sale_order')} for {out.get('partner')}: "
                       f"{out.get('amount')}, delivery {out.get('delivery')} open, "
                       f"invoice {', '.join(out.get('invoices') or []) or 'on shipping'} unpaid")
        return {"summary": summary, "order": out, "listing": result["record"]}

    def update_order(self, fb_order_id: str, ship_to: Optional[dict] = None,
                     tracking: Optional[str] = None, carrier: Optional[str] = None,
                     fund_status: Optional[str] = None,
                     payout_id: Optional[str] = None,
                     fee: Optional[float] = None,
                     payout_amount: Optional[float] = None) -> dict:
        """Bring an imported order in step with Facebook (``fb_order_sync``):
        fill a blank street, write tracking + validate the open delivery,
        record the payout (status, payout id) and Facebook's fee and net
        payout amount. Odoo registers no payment: it is recorded in
        QuickBooks (Receive Payment for the full invoice, Bank Deposit with
        the fee as a negative line) and syncs back to Odoo. Each step runs
        once; repeating it changes nothing."""
        self._require()
        if carrier is not None and tracking is None:
            raise OdooError("carrier needs tracking")
        if payout_id is not None and fund_status is None:
            raise OdooError("payout_id needs fund_status")
        update: dict[str, Any] = {}
        if ship_to:
            update["ship_to"] = self._ship_to(ship_to)
        if tracking is not None:
            update["tracking"] = self._ident(tracking, "tracking", 64)
            if carrier is not None:
                update["carrier"] = self._ident(carrier, "carrier", 32)
        if fund_status is not None:
            fund = self._ident(fund_status, "fund_status", 16).lower()
            if fund not in ("pending", "paid"):
                raise OdooError("fund_status must be pending or paid")
            update["fund_status"] = fund
            if payout_id is not None:
                update["payout_id"] = self._ident(payout_id, "payout_id", 32)
        for key, value in (("fee", fee), ("payout_amount", payout_amount)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise OdooError(f"{key} must be a finite number of 0 or more")
            update[key] = float(value)
        if not update:
            raise OdooError("nothing to update")
        return self._order_view(self.client.execute(
            self._SALE_MODEL, "fb_order_sync", self._order_no(fb_order_id), update))

    def attach_label(self, fb_order_id: str, pdf_path: str,
                     tracking: Optional[str] = None,
                     carrier: Optional[str] = None) -> dict:
        """Attach a Facebook-made label PDF to the order's open delivery,
        queue it on the configured label printer and write the tracking
        (``fb_attach_label``). The file must sit directly in
        :meth:`_label_dir`; the PDF never passes through the agent."""
        self._require()
        order_no = self._order_no(fb_order_id)
        blob = self._read_label(pdf_path)
        kwargs: dict[str, Any] = {}
        if tracking is not None:
            kwargs["tracking"] = self._ident(tracking, "tracking", 64)
        if carrier is not None:
            kwargs["carrier"] = self._ident(carrier, "carrier", 32)
        return self._order_view(self.client.execute(
            self._SALE_MODEL, "fb_attach_label", order_no,
            base64.b64encode(blob).decode("ascii"), **kwargs))

    @staticmethod
    def _discard(fh: Any, name: str, dfd: int) -> None:
        """Best-effort removal of a reserved label file; never raises (a
        cleanup error must not hide what happened to the purchase)."""
        try:
            fh.close()
        except OSError:
            pass
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError:
            pass

    def make_own_label(self, fb_order_id: str) -> dict:
        """Own-label order: buy the label through the configured carrier on
        the open delivery and print it (``fb_make_own_label``). BUYS POSTAGE,
        so the call is never retried automatically. The label PDF goes to a
        file reserved in :meth:`_label_dir` BEFORE the purchase (for the
        Facebook upload), never inline. Anything that goes wrong after the
        purchase comes back as ``save_error``: the postage is bought, do not
        buy again; reprint from the Odoo delivery."""
        self._require()
        order_no = self._order_no(fb_order_id)
        root, dfd = self._open_label_dir()
        name = f"own_label_{order_no}_{uuid.uuid4().hex[:8]}.pdf"
        try:
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=dfd)
            except OSError as exc:
                raise OdooError(f"cannot reserve the label file: {exc.strerror}") from None
            fh = os.fdopen(fd, "wb")
            try:
                raw = self.client.execute_once(self._SALE_MODEL, "fb_make_own_label", order_no)
            except Exception as exc:  # noqa: BLE001 - any failure: outcome unknown
                # Whatever broke (server fault after the carrier call, gateway
                # 504, truncated or unparsable reply), the carrier may already
                # have sold the label: never present it as a clean failure.
                self._discard(fh, name, dfd)
                raise OdooError(
                    f"label purchase outcome UNKNOWN ({type(exc).__name__}: {exc}): "
                    "the postage may have been bought. Check the Odoo delivery's "
                    "tracking before buying again.") from None
            out = self._order_view(raw) if isinstance(raw, dict) else {}
            out["label_path"] = ""
            try:
                if not isinstance(raw, dict):
                    raise ValueError("unexpected server reply")
                b64 = raw.get("pdf_b64") or ""
                if not b64:
                    raise ValueError("the carrier returned no label PDF")
                if not isinstance(b64, str) or len(b64) > self._MAX_LABEL_BYTES * 2:
                    raise ValueError("label too large")
                blob = base64.b64decode(b64, validate=True)
                if not blob.startswith(b"%PDF-") or len(blob) > self._MAX_LABEL_BYTES:
                    raise ValueError("not a PDF")
                fh.write(blob)
                fh.close()
                out["label_path"] = str(root / name)
            except (ValueError, OSError, binascii.Error) as exc:
                out["save_error"] = (f"label bought but not saved locally ({exc}); "
                                     "do NOT buy again: check / reprint from the Odoo delivery")
                self._discard(fh, name, dfd)
            return out
        finally:
            os.close(dfd)

    def mark_renewed(self, listing_id: int) -> dict:
        """Record that the listing was renewed on Facebook, resetting the clock."""
        return self.run_action(listing_id, "action_renewed")

    def mark_pending(self, listing_id: int, sync: bool = True) -> dict:
        """Listed -> pending (a buyer is lined up; the post stays up).

        ``sync=True`` (default) is the operator's instruction: Odoo queues a
        ``mark_pending`` row for the browser half to press Facebook's "Mark
        as pending". ``sync=False`` is the reconcile write-back for a post
        Facebook already shows as pending - state only, nothing queued.
        """
        return self.run_action(listing_id, "action_mark_pending",
                               sync=_strict_bool(sync, "sync"))

    def mark_available(self, listing_id: int, sync: bool = True) -> dict:
        """Pending -> listed (the deal fell through). Same ``sync`` contract
        as :meth:`mark_pending`, for Facebook's "Mark as available"."""
        return self.run_action(listing_id, "action_mark_available",
                               sync=_strict_bool(sync, "sync"))

    def mark_synced(self, listing_id: int, token: str, ok: bool = True,
                    note: Optional[str] = None, action: Optional[str] = None) -> dict:
        """Report the queued Facebook action back (``fb_sync_done``).

        ``token`` is the request id (``fb_sync_token``) the row carried when
        the browser picked it up (from :meth:`sync_queue` or :meth:`get`).
        ``ok=True`` clears the queue row; ``ok=False`` keeps it queued, stores
        ``note`` as ``fb_sync_error`` and hands the listing's creator a to-do.
        ``note`` is truncated server-side to 200 chars. ``action`` names the
        request the browser actually performed (one of :data:`SYNC_ACTIONS`).
        The server ignores the report as stale — ``returned[0]["acknowledged"]``
        is False — when the token (or action) no longer matches the queue,
        i.e. the listing was flipped again while the browser was busy.
        """
        token = str(token or "").strip()
        if not token:
            raise OdooError("token is required (the row's fb_sync_token)")
        kwargs: dict[str, Any] = {"token": token, "ok": _strict_bool(ok, "ok")}
        if note:
            kwargs["note"] = str(note)[:200]
        if action:
            if action not in self.SYNC_ACTIONS:
                raise OdooError(f"action must be one of {', '.join(self.SYNC_ACTIONS)}")
            kwargs["action"] = action
        return self.run_action(listing_id, "fb_sync_done", **kwargs)

    def end_listing(self, listing_id: int) -> dict:
        """Withdraw a listing without a sale."""
        return self.run_action(listing_id, "action_end_listing")

    def reset_draft(self, listing_id: int) -> dict:
        """Send a listing back to draft."""
        return self.run_action(listing_id, "action_reset_draft")

    def generate_content(self, listing_id: int) -> dict:
        """Have the module draft title/description copy with AI.

        Writes into the listing; it does **not** post anything to Facebook.
        """
        result = self.run_action(listing_id, "action_generate_ai_content")
        record = result["record"]
        return {
            "summary": (
                f"AI content generated for '{record.get('name')}'. Review it, "
                "then mark_listed once the post is up."
            ),
            "description": record.get("description"),
            # 4.12: body + template footer, the text to put on Facebook
            # (absent on an older module).
            "description_full": record.get("description_full"),
            "listing": record,
        }

    def apply_suggested_price(self, listing_id: int) -> dict:
        """Accept the module's AI-suggested price for a listing."""
        return self.run_action(listing_id, "action_apply_suggested_price")

    def add_image(
        self, listing_id: int, image_b64: str, caption: Optional[str] = None,
        sequence: int = 10,
    ) -> dict:
        """Attach a photo to a listing.

        Args:
            listing_id: Listing to attach to.
            image_b64: Base64-encoded image payload (no data: URI prefix).
            caption: Optional caption stored on the image row.
            sequence: Display order; lower sorts first.
        """
        self._require()
        values: dict[str, Any] = {
            "listing_id": listing_id,
            "image": image_b64,
            "sequence": sequence,
        }
        if caption:
            values["name"] = caption
        image_id = self.client.create(self.IMAGE_MODEL, values)
        return {
            "summary": f"Photo #{image_id} attached to listing {listing_id}",
            "image_id": image_id,
            "images": self.get_images(listing_id),
        }

    # ── Summary ──────────────────────────────────────────────────────

    # ── Package: weight from a scale, box dimensions ─────────────────

    def scales(self) -> list[dict]:
        """Ventor scales currently online (``product.template.fb_scales``).

        Each row is ``{"id", "name", "computer"}``. Model-level RPC — called
        with no ids list (see :meth:`BaseOps._call_model`). There is no
        default scale on purpose: the caller shows this list and the
        operator picks one per read.
        """
        self._require()
        rows = self.client.execute(_PRODUCT_MODEL, "fb_scales")
        return [dict(r) for r in rows] if isinstance(rows, list) else []

    def boxes(self) -> list[dict]:
        """Warehouse boxes that carry a size, smallest first
        (``product.template.fb_package_types``, fb_marketplace_lister 4.4).

        Each row is ``{"id", "name", "length", "width", "height"}`` in
        inches. Pass a row's ``id`` (or its name / ``"LxWxH"`` size) as
        ``box`` to :meth:`set_package` or ``ebay.stage_listing``.
        """
        self._require()
        try:
            rows = self.client.execute(_PRODUCT_MODEL, "fb_package_types")
        except OdooError as exc:
            if server_lacks_method(exc, "fb_package_types"):
                raise OdooError("Warehouse boxes need fb_marketplace_lister >= 4.4 "
                                "on this server.") from exc
            raise
        if not isinstance(rows, list):
            raise OdooError(f"fb_package_types returned {type(rows).__name__}, expected a list.")
        return [dict(r) for r in rows]

    def _package_target(self, listing_id: Optional[int],
                        product_id: Optional[int]) -> tuple[str, int]:
        """``(model, id)`` the package RPC runs on: the listing when given
        (it delegates to its product), else the product template."""
        if listing_id:
            return self.MODEL, int(listing_id)
        if product_id:
            return _PRODUCT_MODEL, int(product_id)
        raise ValueError("Pass listing_id (fb.marketplace.listing) or "
                         "product_id (product.template).")

    def read_scale(self, listing_id: Optional[int] = None,
                   product_id: Optional[int] = None,
                   scales_id: Optional[int] = None, write: bool = True) -> dict:
        """Weigh the item on a Ventor scale and store the result
        (``fb_read_scale``).

        *scales_id* is one of :meth:`scales`; omitted, the server falls
        back in order to the scale remembered on the product (or wizard),
        then the API user's Ventor default, then the only scale that is
        online — and fails with a plain error when none of those applies.
        Returns the reading (``weight`` in the database's weight unit —
        lb or kg per the ``product.weight_in_lbs`` setting — ``uom``,
        ``scales``, ``measured_on``); with *write* the product's
        ``weight`` and ``weight_measured_on`` are updated in the same
        call.
        """
        model, rec_id = self._package_target(listing_id, product_id)
        kwargs: dict[str, Any] = {"write": bool(write)}
        if scales_id:
            kwargs["scales_id"] = int(scales_id)
        result = self.client.execute(model, "fb_read_scale", [rec_id], **kwargs)
        reading = dict(result) if isinstance(result, dict) else {}
        weight = reading.get("weight")
        uom = reading.get("uom") or "lb"
        return {
            "summary": (
                f"{weight} {uom} on scale {reading.get('scales') or scales_id or '?'}"
                + (" — written to the product." if write else " (not written).")
                if weight is not None else "Scale returned no reading."
            ),
            "reading": reading,
            "written": bool(write) and weight is not None,
            "target": {"model": model, "id": rec_id},
        }

    def set_package(self, listing_id: Optional[int] = None,
                    product_id: Optional[int] = None,
                    weight: Optional[float] = None,
                    length: Optional[float] = None,
                    width: Optional[float] = None,
                    height: Optional[float] = None,
                    box: Any = None) -> dict:
        """Write package weight, box dimensions and/or the warehouse box
        (``fb_set_package``).

        *weight* is stored unchanged into ``product.weight``, so it is in
        the database's weight unit (lb or kg per the
        ``product.weight_in_lbs`` setting); dimensions are inches. *box* is
        a warehouse box by id, name or ``"LxWxH"`` size (see :meth:`boxes`;
        fb_marketplace_lister 4.4): its size fills the dims unless explicit
        dims come in the same call; ``""`` / ``"none"`` clears the box and
        keeps the dims. An ambiguous or unknown box is the server's error,
        naming the candidates.

        Only the values passed are sent, as keywords; the server leaves an
        omitted one unchanged, so a weight-only or dims-only update never
        zeroes the other half. Negative values are refused before any RPC.
        """
        model, rec_id = self._package_target(listing_id, product_id)
        given: dict[str, Any] = {}
        if box is not None:
            given["box"] = _box_arg(box)
        for key, val in (("weight", weight), ("length", length),
                         ("width", width), ("height", height)):
            if val is None:
                continue
            try:
                given[key] = float(val)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number, got {val!r}")
            if given[key] < 0:
                raise ValueError(f"{key} cannot be negative")
        if not given:
            raise ValueError("Pass at least one of weight, length, width, height, box.")
        result = self.client.execute(model, "fb_set_package", [rec_id], **given)
        stored = dict(result) if isinstance(result, dict) else {}
        pkg = {k: stored.get(k, given.get(k)) for k in ("weight", "length", "width", "height")}
        fmt = lambda v: f"{float(v):g}" if v not in (None, False) else "?"  # noqa: E731
        box_note = ""
        if "box" in given:
            box_note = (f", box #{stored['box_id']} {stored.get('box') or ''}".rstrip()
                        if stored.get("box_id") else ", no box")
        return {
            "summary": (
                f"Package on {model} #{rec_id}: {fmt(pkg['weight'])} "
                f"{stored.get('uom') or 'lb'}, {fmt(pkg['length'])}×{fmt(pkg['width'])}"
                f"×{fmt(pkg['height'])} {stored.get('dim_uom') or 'in'}{box_note}."
            ),
            "package": stored or given,
            "target": {"model": model, "id": rec_id},
        }

    def marketplace_summary(self) -> dict:
        """Pipeline counts plus the renewal queue — the daily Marketplace view."""
        counts = {s: self.count([["state", "=", s]]) for s in STATES}
        due = self.count(self._renewal_domain())
        due_soon = self.count(self._renewal_domain(within_days=3))
        stale = self.count_computed(
            [["state", "in", ["listed", "renewal_due"]]],
            lambda r: (r.get("days_listed") or 0) >= 30,
            extra_fields=["days_listed"],
        )
        return {
            "summary": (
                f"Marketplace: {counts['listed']} live, {counts['draft']} draft, "
                f"{due} due for renewal ({due_soon} within 3 days), "
                f"{stale} live 30+ days, {counts['sold']} sold"
            ),
            "by_state": counts,
            "renewal_due": due,
            "renewal_due_soon": due_soon,
            "stale_30d": stale,
        }


def _plain_text(value: Any) -> str:
    """Strip HTML tags/entities from a rich-text field (``description_sale``)."""
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", str(value))
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _differs(a: Any, b: Any, tolerance: float = 0.01) -> bool:
    """Whether two optional prices differ by more than a cent."""
    if a is None or b is None:
        return False
    try:
        cents_a = round(float(a) * 100)
        cents_b = round(float(b) * 100)
    except (TypeError, ValueError):
        return False
    return abs(cents_a - cents_b) > round(tolerance * 100) - 1
