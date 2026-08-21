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

import logging
from datetime import timedelta
from typing import Any, Optional

from ._base import BaseOps, utc_stamp

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "product_tmpl_id", "state", "condition", "price",
    "suggested_price", "listed_date", "renewal_date", "listing_url",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "description", "first_listed_date", "sold_date", "days_listed",
    "days_to_sell", "ai_generated", "is_temp", "image_ids", "currency_id",
    "create_date", "write_uid",
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

    def mark_sold(self, listing_id: int) -> dict:
        """Close a listing as sold."""
        return self.run_action(listing_id, "action_mark_sold")

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


def _differs(a: Any, b: Any, tolerance: float = 0.01) -> bool:
    """Whether two optional prices differ by more than a cent."""
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) > tolerance
    except (TypeError, ValueError):
        return False
