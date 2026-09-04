"""
eBay listing and repricing operations for the ``sale_ebay`` fork.

**Listings live on ``product.template``, not on ``ebay.listing``.** The fork
ships an ``ebay.listing`` model, but its own code notes it is unused in
production: every live listing is a ``product.template`` carrying the ~56
``ebay_*`` fields, published through ``push_product_ebay()`` (eBay Sell
Inventory API). The guided listing wizard exposes that path as a small RPC
surface on the template — ``ebay_wizard_state`` / ``ebay_wizard_save`` /
``ebay_wizard_add_images`` / ``ebay_wizard_push`` — and this class wraps
exactly that surface, so what an agent stages is what the wizard would.

Staging and publishing are separate calls on purpose. :meth:`stage_listing`
prepares a product (enables eBay, copies category policy defaults, fills
title/price/condition/description, copies photos, flips ``sale_ok`` for
Marketplace temp items) and returns the wizard's readiness report.
:meth:`publish` refuses while that report has blockers, and again when the
product is already live, so an unattended worker can never double-list.

**Repricing is proposal-first by design.** ``ebay_suggested_price`` and
``ebay_suggested_discount_pct`` are computed from researched comps, clamped
by a cost floor (``sale_ebay.reducer_min_margin``) and an anchor
(``sale_ebay.reducer_anchor``: low / p25 / median). Reading a suggestion is
free; applying it is a separate, explicit call. :meth:`apply_suggested_price`
refuses to act when the suggestion is absent or equal to the current price,
so an unattended worker cannot churn prices on stale data.
"""

import html
import json
import logging
import re
import statistics
from datetime import datetime, timezone
from typing import Any, Optional

from ._base import BaseOps, OdooActionNotAllowedError

logger = logging.getLogger("odoo_skill")

_PRODUCT_LIST_FIELDS = [
    "id", "name", "default_code", "list_price", "standard_price",
    "ebay_use", "ebay_listed", "ebay_listing_status", "ebay_title",
    "ebay_fixed_price", "ebay_quantity", "ebay_days_listed", "ebay_url",
    "ebay_id",
]

_PRODUCT_DETAIL_FIELDS = _PRODUCT_LIST_FIELDS + [
    "type", "sale_ok", "categ_id", "qty_available", "virtual_available",
    "ebay_category_id", "ebay_item_condition_id", "ebay_condition_description",
    "ebay_best_offer", "ebay_sync_stock", "ebay_template_id",
    "ebay_seller_payment_policy_id", "ebay_seller_return_policy_id",
    "ebay_seller_shipping_policy_id", "ebay_listing_type",
    "ebay_listing_duration", "product_image_ids",
]

_COMP_FIELDS = [
    "id", "name", "ebay_title", "ebay_fixed_price", "standard_price",
    "ebay_comp_count", "ebay_comp_low", "ebay_comp_p25", "ebay_comp_median",
    "ebay_comp_high", "ebay_comp_note", "ebay_comp_fetched_at",
    "ebay_suggested_price", "ebay_suggested_discount_pct",
    "ebay_days_listed", "ebay_listed", "ebay_url",
]

#: ``product.template.ebay_listing_status`` values that mean "live on eBay"
#: (mirrors ``_EBAY_LIVE_STATUSES`` in the wizard). Pushing any of these
#: again would create a duplicate listing.
LIVE_STATUSES = ("Active", "Out Of Stock")

#: Every ``ebay_listing_status`` the fork writes.
LISTING_STATUSES = ["Active", "Unlisted", "Ended", "Out Of Stock"]

#: eBay's title limit; ``ebay_title`` is ``size=80`` in the fork.
EBAY_TITLE_MAX = 80

#: FB Marketplace listing condition → eBay ``ebay.item.condition.code``.
#: Kept to the codes every category accepts (1000 / 2500 / 3000 / 7000);
#: the finer-grained "Like New" / "Pre-owned - Excellent" codes are
#: category-dependent and would fail on a mismatch. The nuance goes into
#: ``ebay_condition_description`` instead.
FB_CONDITION_TO_EBAY = {
    "new": ("1000", None),
    "refurbished": ("2500", None),
    "like_new": ("3000", "Like new. Tested and fully working."),
    "good": ("3000", None),
    "fair": ("3000", "Used, in fair condition. See photos for wear."),
    "for_parts": ("7000", None),
}

#: Policy / template fields the category copy may fill (blank-only).
_POLICY_FIELDS = (
    "ebay_category_id", "ebay_store_category_id", "ebay_template_id",
    "ebay_seller_payment_policy_id", "ebay_seller_return_policy_id",
    "ebay_seller_shipping_policy_id",
)

#: Fallback keys → product fields (see :meth:`stage_listing` ``fallback``).
_FALLBACK_KEYS = {
    "payment_policy_id": "ebay_seller_payment_policy_id",
    "return_policy_id": "ebay_seller_return_policy_id",
    "shipping_policy_id": "ebay_seller_shipping_policy_id",
    "template_id": "ebay_template_id",
}

_SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _m2o_id(value: Any) -> int | bool:
    """``[id, name]`` / ``{'id':..}`` / int → id (False when unset)."""
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, dict) and value.get("id"):
        return int(value["id"])
    if isinstance(value, int) and value:
        return value
    return False


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    if isinstance(value, dict):
        return str(value.get("name") or value.get("display_name") or "")
    return ""


def _text_to_html(text: str) -> str:
    """Plain text (FB description) → minimal safe HTML for ``ebay_description``.

    Blank-line separated blocks become ``<p>``; single newlines ``<br/>``.
    Everything is escaped — an agent-typed description is never trusted as
    markup.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    return "".join(
        "<p>" + "<br/>".join(html.escape(line) for line in block.split("\n")) + "</p>"
        for block in blocks
    )


class EbayListingOps(BaseOps):
    """Listing lifecycle (stage → publish → end) and comp-driven repricing."""

    MODEL = "product.template"
    MODULE = "sale_ebay"
    #: Kept for callers that referenced it; same model now.
    PRODUCT_MODEL = "product.template"
    FB_LISTING_MODEL = "fb.marketplace.listing"
    FB_IMAGE_MODEL = "fb.marketplace.listing.image"
    LIST_FIELDS = _PRODUCT_LIST_FIELDS
    DETAIL_FIELDS = _PRODUCT_DETAIL_FIELDS
    ORDER = "id desc"
    REQUIRED_GROUPS = ("sales_team.group_sale_salesman",)

    #: Button methods on ``product.template`` :meth:`run_action` may invoke.
    ALLOWED_ACTIONS = frozenset({
        "action_ebay_research_comps",
        "action_end_single_listing",
        "action_ebay_listing_per_variant",
    })
    #: Alias so existing callers of :meth:`run_product_action` keep working.
    ALLOWED_PRODUCT_ACTIONS = ALLOWED_ACTIONS

    # ── Lookup ───────────────────────────────────────────────────────

    def active_listings(self, limit: int = 100) -> list[dict]:
        """Products currently live on eBay (Active or Out Of Stock)."""
        return self.search(
            [["ebay_listing_status", "in", list(LIVE_STATUSES)]], limit=limit)

    def unlisted_ready(self, limit: int = 100) -> list[dict]:
        """eBay-enabled products that are not live (staged but unpublished)."""
        return self.search([
            ["ebay_use", "=", True],
            ["ebay_listing_status", "not in", list(LIVE_STATUSES)],
        ], limit=limit)

    def listings_for_product(self, product_tmpl_id: int) -> list[dict]:
        """The product's own eBay row (a template IS the listing here)."""
        return self.search([["id", "=", product_tmpl_id]], limit=1)

    def resolve_item(self, ref: str, limit: int = 10) -> dict:
        """Turn an agent-typed reference into a ``product.template`` id.

        Accepted forms, in the order they are tried:

        * ``"12"`` / ``"fb 12"`` / ``"fb:12"`` — an **FB Marketplace listing
          id** (``fb.marketplace.listing``); resolves to its product. A bare
          integer is *always* an FB listing id, never a product id.
        * ``"FBM-00012"`` / ``"sku FBM-00012"`` — an exact ``default_code``.
        * anything else — a case-insensitive **name search**; returns
          ``candidates`` for the caller to disambiguate, ``product_tmpl_id``
          only when exactly one matches.

        Returns ``{"kind", "product_tmpl_id", "fb_listing_id", "candidates",
        "summary"}``. Never raises on a miss — ``product_tmpl_id`` is
        ``None`` and ``summary`` says why.
        """
        self._require()
        raw = (ref or "").strip()
        m = re.match(r"^(?:fb[:\s#]*)?(\d+)$", raw, re.I)
        if m:
            fb_id = int(m.group(1))
            rows = self.client.search_read(
                self.FB_LISTING_MODEL, [["id", "=", fb_id]],
                fields=["id", "name", "product_tmpl_id", "state", "condition"],
                limit=1,
            )
            if not rows:
                return {
                    "kind": "fb", "product_tmpl_id": None, "fb_listing_id": fb_id,
                    "candidates": [],
                    "summary": f"No FB Marketplace listing #{fb_id}.",
                }
            fb = rows[0]
            tmpl_id = _m2o_id(fb.get("product_tmpl_id"))
            return {
                "kind": "fb", "product_tmpl_id": tmpl_id or None,
                "fb_listing_id": fb_id, "candidates": [],
                "fb_listing": fb,
                "summary": (
                    f"FB listing #{fb_id} '{fb.get('name')}' → product "
                    f"{tmpl_id}" if tmpl_id else
                    f"FB listing #{fb_id} has no product."),
            }
        m = re.match(r"^(?:sku[:\s]*)?(\S+)$", raw, re.I)
        token = m.group(1) if m else raw
        if _SKU_RE.match(token) and (raw.lower().startswith("sku") or "-" in token
                                     or token.upper() == token):
            rows = self.client.search_read(
                self.MODEL, [["default_code", "=ilike", token]],
                fields=self._fields(), limit=2,
            )
            if len(rows) == 1:
                return {
                    "kind": "sku", "product_tmpl_id": rows[0]["id"],
                    "fb_listing_id": None, "candidates": rows,
                    "summary": f"SKU {token} → product {rows[0]['id']} "
                               f"'{rows[0].get('name')}'",
                }
            if len(rows) > 1:
                return {
                    "kind": "sku", "product_tmpl_id": None, "fb_listing_id": None,
                    "candidates": rows,
                    "summary": f"SKU {token} matches {len(rows)} products.",
                }
        rows = self.client.search_read(
            self.MODEL, [["name", "ilike", raw]],
            fields=self._fields(), limit=limit, order="name",
        )
        if len(rows) == 1:
            return {
                "kind": "name", "product_tmpl_id": rows[0]["id"],
                "fb_listing_id": None, "candidates": rows,
                "summary": f"'{raw}' → product {rows[0]['id']} "
                           f"'{rows[0].get('name')}'",
            }
        return {
            "kind": "name", "product_tmpl_id": None, "fb_listing_id": None,
            "candidates": rows,
            "summary": (f"'{raw}' matches {len(rows)} products — pick one."
                        if rows else f"Nothing matches '{raw}'."),
        }

    def search_category(self, term: str, limit: int = 20) -> list[dict]:
        """Leaf eBay categories whose name matches *term* (primary tree)."""
        self._require()
        return self.client.search_read(
            "ebay.category",
            [["category_type", "=", "ebay"], ["leaf_category", "=", True],
             ["name", "ilike", term]],
            fields=["id", "name", "display_name", "category_id"],
            limit=limit,
        )

    def condition_choices(self) -> list[dict]:
        """Every ``ebay.item.condition`` (id, code, name)."""
        self._require()
        return self.client.search_read(
            "ebay.item.condition", [], fields=["id", "code", "name"], order="code")

    def condition_id_for_code(self, code: str) -> int | None:
        rows = self.client.search_read(
            "ebay.item.condition", [["code", "=", str(code)]], fields=["id"], limit=1)
        return rows[0]["id"] if rows else None

    def listing_state(self, product_tmpl_id: int) -> dict:
        """The wizard's full view: fields, choices, photos, specifics, readiness."""
        self._require()
        return self.client.execute(self.MODEL, "ebay_wizard_state", [product_tmpl_id])

    def readiness(self, product_tmpl_id: int) -> dict:
        """Just the readiness block (blockers / warnings / can_push)."""
        state = self.listing_state(product_tmpl_id)
        r = dict(state.get("readiness") or {})
        r["product"] = state.get("product")
        if r.get("can_push"):
            r["summary"] = "Ready to publish."
        elif r.get("already_listed"):
            r["summary"] = "Already listed."
        else:
            r["summary"] = "Blocked: " + ("; ".join(r.get("blockers") or []) or "not ready")
        return r

    # ── Staging ──────────────────────────────────────────────────────

    def set_listing_fields(self, product_tmpl_id: int, vals: dict,
                           description: Optional[str] = None) -> dict:
        """Write eBay listing fields through the wizard's whitelist.

        ``vals`` keys are the wizard-editable fields (title, condition,
        price, quantity, category, policies, template, best offer, stock
        sync…); anything else is silently dropped by Odoo. ``ebay_title`` is
        clipped to 80 characters. ``description`` is plain text or HTML and
        goes to ``ebay_description`` (not wizard-editable, so written
        directly); text is converted to escaped ``<p>`` markup.
        """
        self._require()
        clean = dict(vals or {})
        if clean.get("ebay_title"):
            clean["ebay_title"] = str(clean["ebay_title"]).strip()[:EBAY_TITLE_MAX]
        state = self.client.execute(
            self.MODEL, "ebay_wizard_save", [product_tmpl_id], clean)
        if description is not None:
            body = description if "<" in description and ">" in description \
                else _text_to_html(description)
            self.client.write(self.MODEL, product_tmpl_id, {"ebay_description": body})
        return state

    def set_category_defaults(self, product_tmpl_id: int,
                              categ_id: Optional[int] = None) -> dict:
        """Copy the product category's eBay defaults into blank fields only.

        Optionally moves the product to *categ_id* first (a category with
        eBay defaults configured — see ``odoo-ebay-custom``). The fork's
        ``apply_ebay_policies_from_category`` overwrites; this snapshots the
        already-set policy fields and restores them, so a value chosen by
        hand is never clobbered. Returns the resulting policy field ids.
        """
        self._require()
        if categ_id:
            self.client.write(self.MODEL, product_tmpl_id, {"categ_id": int(categ_id)})
        before = self.client.read(
            self.MODEL, [product_tmpl_id], fields=list(_POLICY_FIELDS))[0]
        preset = {f: _m2o_id(before.get(f)) for f in _POLICY_FIELDS if _m2o_id(before.get(f))}
        self.client.execute(self.MODEL, "apply_ebay_policies_from_category", [product_tmpl_id])
        after = self.client.read(
            self.MODEL, [product_tmpl_id], fields=list(_POLICY_FIELDS))[0]
        restore = {f: pid for f, pid in preset.items() if _m2o_id(after.get(f)) != pid}
        if restore:
            self.client.write(self.MODEL, product_tmpl_id, restore)
            after.update({f: [pid, ""] for f, pid in restore.items()})
        return {f: _m2o_id(after.get(f)) for f in _POLICY_FIELDS}

    def add_images(self, product_tmpl_id: int, images: list[dict]) -> dict:
        """Append photos: ``[{"datas": <base64>, "name": <str>}, …]``."""
        self._require()
        return self.client.execute(
            self.MODEL, "ebay_wizard_add_images", [product_tmpl_id], images)

    def add_images_from_fb(self, product_tmpl_id: int, fb_listing_id: int,
                           force: bool = False) -> dict:
        """Copy an FB Marketplace listing's photos onto the product gallery.

        eBay publishes ``product_image_ids`` (the gallery), while FB photos
        live on ``fb.marketplace.listing.image``. Skipped when the product
        already has gallery photos unless *force* — re-running staging must
        not duplicate the gallery.
        """
        self._require()
        tmpl = self.client.read(
            self.MODEL, [product_tmpl_id], fields=["product_image_ids"])[0]
        existing = tmpl.get("product_image_ids") or []
        if existing and not force:
            return {"copied": 0, "skipped": True,
                    "summary": f"Product already has {len(existing)} gallery photo(s); not copied."}
        rows = self.client.search_read(
            self.FB_IMAGE_MODEL, [["listing_id", "=", fb_listing_id]],
            fields=["id", "name", "sequence", "image"], order="sequence, id", limit=24)
        payload = [
            {"datas": r["image"], "name": r.get("name") or f"FB photo {r['id']}"}
            for r in rows if r.get("image")
        ]
        if not payload:
            return {"copied": 0, "skipped": False,
                    "summary": f"FB listing #{fb_listing_id} has no photos to copy."}
        self.client.execute(self.MODEL, "ebay_wizard_add_images", [product_tmpl_id], payload)
        return {"copied": len(payload), "skipped": False,
                "summary": f"Copied {len(payload)} photo(s) from FB listing #{fb_listing_id}."}

    def stage_listing(
        self,
        product_tmpl_id: int,
        vals: Optional[dict] = None,
        description: Optional[str] = None,
        categ_id: Optional[int] = None,
        fb_listing_id: Optional[int] = None,
        fallback: Optional[dict] = None,
        best_offer: bool = True,
        sync_stock: bool = True,
    ) -> dict:
        """Prepare a product for eBay and report readiness. Does NOT publish.

        Steps, each idempotent:

        1. ``ebay_use = True`` on the template and every variant (push
           filters variants on it); ``sale_ok = True`` (Marketplace temp
           items are created unsaleable — eBay orders need a saleable
           product).
        2. Category defaults into blank fields (:meth:`set_category_defaults`,
           optionally re-categorising to *categ_id* first), then
           *fallback* — ``{"payment_policy_id", "return_policy_id",
           "shipping_policy_id", "template_id"}`` — into any still blank.
        3. Listing defaults: FixedPriceItem / GTC, ``ebay_fixed_price`` from
           ``list_price``, title from name, best offer, stock sync (storable
           products only) with ``ebay_quantity`` from on-hand stock.
        4. When *fb_listing_id* is given: condition mapped from the FB
           listing (:data:`FB_CONDITION_TO_EBAY`), description and title from
           it when not supplied, photos copied to the gallery.
        5. Caller's *vals* / *description* last, so they win.

        Returns the wizard state plus ``readiness`` and ``staged`` (what was
        written). Publishing is :meth:`publish`, a separate confirmed step.
        """
        self._require()
        tmpl = self.client.read(
            self.MODEL, [product_tmpl_id], fields=self._fields(detail=True))
        if not tmpl:
            from ..errors import OdooRecordNotFoundError
            raise OdooRecordNotFoundError(
                f"No product.template with id {product_tmpl_id}")
        tmpl = tmpl[0]
        staged: dict[str, Any] = {}
        notes: list[str] = []
        if tmpl.get("ebay_listing_status") in LIVE_STATUSES:
            state = self.listing_state(product_tmpl_id)
            state["staged"] = {}
            state["notes"] = [f"Already live on eBay ({tmpl['ebay_listing_status']}); nothing staged."]
            return state

        # 1. enable + saleable
        base: dict[str, Any] = {}
        if not tmpl.get("ebay_use"):
            base["ebay_use"] = True
        if not tmpl.get("sale_ok"):
            base["sale_ok"] = True
        if base:
            self.client.write(self.MODEL, product_tmpl_id, base)
            staged.update(base)
        variant_ids = self.client.search(
            "product.product", [["product_tmpl_id", "=", product_tmpl_id],
                                ["ebay_use", "=", False]])
        if variant_ids:
            self.client.write("product.product", variant_ids, {"ebay_use": True})
            staged["variant_ebay_use"] = len(variant_ids)

        # 2. category defaults, then fallback
        policies = self.set_category_defaults(product_tmpl_id, categ_id)
        if categ_id:
            staged["categ_id"] = categ_id
        fill: dict[str, Any] = {}
        for key, field in _FALLBACK_KEYS.items():
            if not policies.get(field) and (fallback or {}).get(key):
                fill[field] = int(fallback[key])
        if fill:
            notes.append("Fallback policies used for: " + ", ".join(sorted(fill)))

        # 3. listing defaults
        fb: dict = {}
        if fb_listing_id:
            rows = self.client.search_read(
                self.FB_LISTING_MODEL, [["id", "=", fb_listing_id]],
                fields=["id", "name", "description", "condition", "price",
                        "product_tmpl_id"], limit=1)
            if rows:
                fb = rows[0]
                if _m2o_id(fb.get("product_tmpl_id")) not in (False, product_tmpl_id):
                    notes.append(f"FB listing #{fb_listing_id} belongs to another product; "
                                 "its details were not used.")
                    fb = {}
        defaults: dict[str, Any] = {}
        if not tmpl.get("ebay_listing_type"):
            defaults["ebay_listing_type"] = "FixedPriceItem"
        if not tmpl.get("ebay_listing_duration"):
            defaults["ebay_listing_duration"] = "GTC"
        if not tmpl.get("ebay_title"):
            defaults["ebay_title"] = (fb.get("name") or tmpl.get("name") or "")
        if not tmpl.get("ebay_fixed_price"):
            price = fb.get("price") or tmpl.get("list_price") or 0.0
            if price:
                defaults["ebay_fixed_price"] = price
        defaults["ebay_best_offer"] = bool(best_offer)
        storable = tmpl.get("type") == "product"
        if sync_stock and storable:
            defaults["ebay_sync_stock"] = True
            on_hand = int(tmpl.get("virtual_available") or tmpl.get("qty_available") or 0)
            if on_hand > 0:
                defaults["ebay_quantity"] = on_hand
            else:
                notes.append("No stock on hand — quantity left as-is; publish will block until stock exists.")
        elif sync_stock and not storable:
            notes.append("Not a storable product — stock sync left off.")
        if fb and not tmpl.get("ebay_item_condition_id"):
            code, cond_desc = FB_CONDITION_TO_EBAY.get(fb.get("condition") or "", (None, None))
            if code:
                cid = self.condition_id_for_code(code)
                if cid:
                    defaults["ebay_item_condition_id"] = cid
                    if cond_desc and not tmpl.get("ebay_condition_description"):
                        defaults["ebay_condition_description"] = cond_desc
                else:
                    notes.append(f"eBay condition code {code} not found on this database.")
        defaults.update(fill)
        defaults.update(vals or {})
        if description is None and fb.get("description"):
            description = fb["description"]
        state = self.set_listing_fields(product_tmpl_id, defaults, description)
        staged.update(defaults)
        if description is not None:
            staged["ebay_description"] = True

        # 4. photos
        if fb_listing_id and fb:
            copied = self.add_images_from_fb(product_tmpl_id, fb_listing_id)
            notes.append(copied["summary"])
            if copied["copied"]:
                state = self.listing_state(product_tmpl_id)

        state["staged"] = staged
        state["notes"] = notes
        state["summary"] = (
            f"Staged product {product_tmpl_id} '{tmpl.get('name')}': "
            + ("ready to publish." if (state.get("readiness") or {}).get("can_push")
               else "blocked — " + "; ".join((state.get("readiness") or {}).get("blockers") or []))
        )
        return state

    # ── Publish / end ────────────────────────────────────────────────

    def publish(self, product_tmpl_id: int) -> dict:
        """Publish the product to eBay (live API call, publicly visible).

        Runs the wizard's readiness check first and refuses on any blocker
        or when the product is already live, then calls
        ``ebay_wizard_push`` (→ ``push_product_ebay``). Returns
        ``{"published", "ebay_url", "status", "summary"}``; a refused or
        failed push is ``published: False`` with the reason, never an
        exception, so the caller can relay it verbatim.
        """
        self._require()
        ready = self.readiness(product_tmpl_id)
        if ready.get("already_listed"):
            rec = self.client.read(self.MODEL, [product_tmpl_id],
                                   fields=["ebay_url", "ebay_listing_status"])[0]
            return {"published": False, "reason": "already_listed",
                    "ebay_url": rec.get("ebay_url") or None,
                    "status": rec.get("ebay_listing_status"),
                    "summary": f"Already listed on eBay ({rec.get('ebay_listing_status')})."}
        if not ready.get("can_push"):
            return {"published": False, "reason": "not_ready",
                    "blockers": ready.get("blockers") or [],
                    "summary": ready["summary"]}
        result = self.client.execute(self.MODEL, "ebay_wizard_push", [product_tmpl_id])
        if not isinstance(result, dict):
            result = {"success": bool(result)}
        if not result.get("success"):
            return {"published": False,
                    "reason": result.get("error") or "push_failed",
                    "ebay_url": result.get("ebay_url") or None,
                    "summary": result.get("message") or "eBay push failed."}
        return {
            "published": True,
            "ebay_url": result.get("ebay_url") or None,
            "status": result.get("ebay_listing_status"),
            "summary": f"Published to eBay: {result.get('ebay_url') or '(no URL returned)'}",
        }

    def end_listing(self, product_tmpl_id: int) -> dict:
        """End the product's live eBay listing (``action_end_single_listing``)."""
        result = self.run_action(product_tmpl_id, "action_end_single_listing")
        rec = result["record"]
        return {
            "summary": f"{rec.get('name')} → {rec.get('ebay_listing_status')}",
            "status": rec.get("ebay_listing_status"),
            "record": rec,
        }

    # ── Repricing (proposal-first) ───────────────────────────────────

    def research_comps(self, product_tmpl_id: int) -> dict:
        """Refresh competitor comps for a product, then report the suggestion.

        Calls ``action_ebay_research_comps`` (an eBay Browse API search) and
        reads back the recomputed comp aggregates and suggested price.
        """
        self._require()
        self.client.execute(self.MODEL, "action_ebay_research_comps", [product_tmpl_id])
        return self.get_pricing(product_tmpl_id)

    def set_sold_comps(self, product_tmpl_id: int, prices: list[float],
                       source: str = "ebay_sold_browser",
                       listings: Optional[list[dict]] = None) -> dict:
        """Store externally gathered comps (e.g. sold prices read from eBay in
        a browser) as the product's comp aggregates.

        The Browse API only sees *asking* prices; sold/completed prices need a
        browser. Writes ``ebay_comp_low/p25/median/high/count/fetched_at`` and
        a JSON note of the raw prices; ``ebay_suggested_price`` and
        ``ebay_comp_note`` are Odoo-computed from these, so the cost floor
        and anchor rules still apply. Refuses on fewer than 3 prices.
        """
        self._require()
        clean = sorted(float(p) for p in (prices or []) if p and float(p) > 0)
        if len(clean) < 3:
            return {"written": False,
                    "summary": f"Need at least 3 sold prices, got {len(clean)}."}
        q = statistics.quantiles(clean, n=4) if len(clean) >= 4 else [clean[0]] * 3
        vals = {
            "ebay_comp_low": clean[0],
            "ebay_comp_p25": round(q[0], 2),
            "ebay_comp_median": round(statistics.median(clean), 2),
            "ebay_comp_high": clean[-1],
            "ebay_comp_count": len(clean),
            "ebay_comp_fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "ebay_comp_json": json.dumps({
                "source": source, "prices": clean,
                "listings": listings or [],
            }),
        }
        self.client.write(self.MODEL, product_tmpl_id, vals)
        pricing = self.get_pricing(product_tmpl_id)
        pricing["written"] = True
        pricing["summary"] = (
            f"{len(clean)} sold comps: low {clean[0]:.2f} / median "
            f"{vals['ebay_comp_median']:.2f} / high {clean[-1]:.2f}. " + pricing["summary"])
        return pricing

    def get_pricing(self, product_tmpl_id: int) -> dict:
        """Read the current comp aggregates and price suggestion for a product."""
        self._require()
        rows = self.client.read(
            self.MODEL, [product_tmpl_id], fields=_COMP_FIELDS
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
            self.MODEL, domain, fields=_COMP_FIELDS,
            limit=limit, order="ebay_suggested_discount_pct desc",
        )

    def stale_comps(self, limit: int = 100) -> list[dict]:
        """Listed products whose comps have never been fetched.

        Feed these to :meth:`research_comps` before trusting any suggestion.
        """
        self._require()
        return self.client.search_read(
            self.MODEL,
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
            self.MODEL, product_tmpl_id,
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
        """Invoke an allowlisted eBay method on a ``product.template``.

        Same as :meth:`run_action` (the model is the template now); kept so
        existing callers and the frozen method inventory keep working.
        """
        if method not in self.ALLOWED_PRODUCT_ACTIONS:
            raise OdooActionNotAllowedError(
                f"Method '{method}' is not permitted on {self.MODEL}. "
                f"Allowed: {', '.join(sorted(self.ALLOWED_PRODUCT_ACTIONS))}"
            )
        return self.run_action(product_tmpl_id, method, **kwargs)

    # ── Summary ──────────────────────────────────────────────────────

    def listing_summary(self) -> dict:
        """Listing counts by status plus the size of the repricing work-list."""
        self._require()
        by_status = {
            s: self.client.search_count(
                self.MODEL, [["ebay_listing_status", "=", s]])
            for s in LISTING_STATUSES
        }
        listed = self.client.search_count(
            self.MODEL, [["ebay_listed", "=", True]]
        )
        candidates = self.client.search_count(
            self.MODEL,
            [["ebay_listed", "=", True], ["ebay_suggested_discount_pct", ">", 0]],
        )
        never_researched = self.client.search_count(
            self.MODEL,
            [["ebay_listed", "=", True], ["ebay_comp_fetched_at", "=", False]],
        )
        return {
            "summary": (
                f"eBay: {by_status['Active']} active, "
                f"{by_status['Out Of Stock']} out of stock, "
                f"{by_status['Unlisted']} unlisted, {by_status['Ended']} ended; "
                f"{listed} products listed. "
                f"{candidates} repricing candidates, "
                f"{never_researched} never researched"
            ),
            "listings_by_status": by_status,
            "products_listed": listed,
            "repricing_candidates": candidates,
            "never_researched": never_researched,
        }
