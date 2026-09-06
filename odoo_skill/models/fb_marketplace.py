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

import html
import logging
import re
import uuid
from datetime import timedelta
from typing import Any, Optional

from ..errors import (
    OdooAccessError, OdooAuthenticationError, OdooConnectionError, OdooError,
)
from ._base import BaseOps, utc_stamp

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "product_tmpl_id", "state", "condition", "price",
    "suggested_price", "listed_date", "renewal_date", "listing_url",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "description", "first_listed_date", "sold_date", "days_listed",
    "days_to_sell", "ai_generated", "is_temp", "image_ids", "currency_id",
    "create_date", "write_uid", "location_id", "shippable",
    # fb_marketplace_lister 4.x per-sale history; dropped by _existing()
    # on databases still running an older module.
    "sold_price", "sold_qty", "sale_count", "can_record_sale",
]

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
STATES = ["draft", "listed", "renewal_due", "sold", "ended"]

#: States a listing is still working in — not yet sold or withdrawn.
OPEN_STATES = ["draft", "listed", "renewal_due"]

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
    })

    # ── Reads ────────────────────────────────────────────────────────

    def active_listings(self, limit: int = 50) -> list[dict]:
        """Listings currently live on Marketplace."""
        return self.search([["state", "=", "listed"]], limit=limit)

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
    ) -> dict:
        """Record a Facebook sale on a listing (``fb_record_sale``).

        Moves ``qty`` units out of stock at ``price`` each (tax-inclusive
        buyer price; ``None`` = list price, ``0`` = giveaway). ``invoice=True``
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
        result = self.run_action(listing_id, "fb_record_sale", **kwargs)
        sale = result["returned"] if isinstance(result["returned"], dict) else {}
        record = result["record"]
        if sale.get("duplicate"):
            summary = f"Ref {ref!r} already recorded on listing #{listing_id}; nothing moved."
        else:
            summary = (
                f"Sale recorded on '{record.get('name')}': {sale.get('qty', qty)} × "
                f"{sale.get('price', price if price is not None else record.get('price'))}"
                + (f", order {sale.get('sale_order')}" if sale.get("sale_order") else "")
                + ("; listing closed" if sale.get("closed") else
                   f"; {sale.get('remaining', '?')} left, listing stays live")
            )
        return {"summary": summary, "sale": sale, "listing": record}

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

    def mark_renewed(self, listing_id: int) -> dict:
        """Record that the listing was renewed on Facebook, resetting the clock."""
        return self.run_action(listing_id, "action_renewed")

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
