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

from ..errors import OdooError, OdooRecordNotFoundError, server_lacks_method
from ._base import BaseOps, OdooActionNotAllowedError
from .fb_marketplace import _box_arg

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
    # package (sale_ebay 1.40): weight is core, in lb on this database
    "weight", "ebay_pkg_length_in", "ebay_pkg_width_in", "ebay_pkg_height_in",
    # text the package fallback parses when the fields are blank
    "description_sale", "ebay_description",
    # policy overrides (odoo-ebay-custom 1.16); dropped by _existing()
    # where that module is older
    "ebay_shipping_mode", "ebay_return_mode", "ebay_warranty_kind",
    "ebay_warranty_months", "ebay_policy_manual",
]

#: ``product.template`` package fields, in ``(weight, L, W, H)`` order.
_PACKAGE_FIELDS = ("weight", "ebay_pkg_length_in", "ebay_pkg_width_in",
                   "ebay_pkg_height_in")

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

#: Policy override values (odoo-ebay-custom ``ebay_*_mode`` selections).
SHIPPING_MODES = ("auto", "free", "calculated", "freight")
RETURN_MODES = ("auto", "accept", "none")

#: ``warranty`` argument → (``ebay_warranty_kind``, ``ebay_warranty_months``).
#: The label text and the eBay "Warranty" item specific are derived server-
#: side from these two fields, so text and specific cannot disagree.
WARRANTY_CHOICES = {
    "auto": ("auto", 0),
    "none": ("none", 0),
    "factory": ("factory", 0),
    "30d": ("months", 1),
    "1y": ("months", 12),
    "2y": ("months", 24),
    "3y": ("months", 36),
}

_KG_TO_LB = 2.2046226218

_WEIGHT_RE = re.compile(
    r"(?:weight|weighs|wt)\s*[:=\-]?\s*(?:about|approx\.?|approximately|~)?\s*"
    r"(\d+(?:\.\d+)?)\s*(lbs?|pounds?|oz|ounces?|kgs?|kilograms?)\b"
    r"(?:\s*(?:and\s*)?(\d+(?:\.\d+)?)\s*(oz|ounces?)\b)?",
    re.I,
)
_NUM = r"(\d+(?:\.\d+)?)"
#: A complete unit token per axis — ``in``/``inch``/``inches``/``"``/``″``,
#: ``cm``, ``mm`` — matched case-insensitively and only as a whole word, so
#: ``20 inside`` is not ``20 in`` and ``CM`` counts.
_UNIT = r'(in\b|inch(?:es)?\b|"|″|cm\b|mm\b)?'
_DIMS_RE = re.compile(
    rf"{_NUM}\s*{_UNIT}\s*[x×]\s*{_NUM}\s*{_UNIT}\s*[x×]\s*{_NUM}\s*{_UNIT}", re.I)
#: Inches per unit token (lower-cased, first letter/glyph is enough).
_UNIT_DIVISOR = {"i": 1.0, '"': 1.0, "″": 1.0, "c": 2.54, "m": 25.4}
_DIMS_CUE_RE = re.compile(
    r"(dimension|dims?\b|size|box|package|measures|shipping|\bL\s*[x×]\s*W)", re.I)

#: Trailing FB-copy sentences that must never reach an eBay description.
_FB_PICKUP_SENTENCE = r"local\s+pickup(?:\s+only)?(?:\s+available)?\s*[.!]?"
_FB_QUESTIONS_SENTENCE = r"message\s+(?:me\s+)?with\s+any\s+questions\s*[.!]?"
_FB_TRAILER_RE = re.compile(
    rf"(?:\s*(?:{_FB_PICKUP_SENTENCE}|{_FB_QUESTIONS_SENTENCE}))+\s*$", re.I)
_FB_TRAILER_LINE_RE = re.compile(
    rf"^\s*(?:{_FB_PICKUP_SENTENCE}\s*)?(?:{_FB_QUESTIONS_SENTENCE})?\s*$", re.I)


def _strip_fb_pickup(text: str) -> str:
    """Drop the FB copy's ``Local pickup. Message with any questions.``
    trailer (either sentence, any order, own line or end of the last
    paragraph). eBay ships; the sentence is wrong there and the AI copy
    used to append it unconditionally."""
    if not text:
        return text
    lines = text.split("\n")
    kept = [ln for ln in lines
            if not (ln.strip() and _FB_TRAILER_LINE_RE.match(ln)
                    and re.search(r"[A-Za-z]", ln))]
    out = "\n".join(kept)
    out = _FB_TRAILER_RE.sub("", out)
    return out.strip("\n ").rstrip() if out.strip() else ""


def _strip_html(text: str) -> str:
    text = re.sub(r"<(?:style|script)\b[^>]*>.*?</(?:style|script)>", " ", text,
                  flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _parse_dims(value: Any) -> Optional[tuple[float, float, float]]:
    """``"18x12x6"`` / ``"18 × 12 × 6 in"`` / ``[18, 12, 6]`` → inches.

    Raises ``ValueError`` on anything else; ``None`` in → ``None`` out.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = re.split(r"\s*[x×X]\s*", str(value).strip())
    if len(parts) != 3:
        raise ValueError(f"dims must be three numbers L x W x H (inches), got {value!r}")
    nums = []
    for part in parts:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(?:in(?:ch(?:es)?)?|\"|″)?\s*$", str(part))
        if not m:
            raise ValueError(f"dims must be three numbers L x W x H (inches), got {value!r}")
        nums.append(float(m.group(1)))
    if any(n <= 0 for n in nums):
        raise ValueError("dims must all be greater than zero")
    return nums[0], nums[1], nums[2]


def _parse_package_text(texts: list[str]) -> dict:
    """Find a package weight (→ lb) and box dimensions (→ inches) in free
    text — product/eBay/FB descriptions — so an item whose fields are blank
    can still ship with what the copy already says.

    Weight needs the word ``weight``/``weighs`` before the number (a bare
    ``36oz`` may be a bottle size; ``font-weight: 700`` has no unit).
    Dimensions need three numbers joined by ``x``/``×`` and either an
    in/cm/mm unit or a nearby cue word (dimensions, size, box, package…) —
    ``1920 x 1080 x 60`` in a spec table must not become a box. Units are
    converted per axis (an axis without its own unit takes the nearest
    later one, so ``40 x 30 x 20 cm`` is all cm); a trailing unit the
    parser does not know (``m``, ``ft``, ``Hz``…) rejects the match with a
    warning rather than guessing inches. First hit wins per value; returns
    ``{"weight_lb", "dims", "weight_text", "dims_text", "warnings"}`` with
    ``None`` for what was not found.
    """
    out: dict[str, Any] = {"weight_lb": None, "dims": None,
                           "weight_text": None, "dims_text": None,
                           "warnings": []}
    for raw in texts:
        if not raw or not isinstance(raw, str):
            continue
        text = _strip_html(raw) if "<" in raw and ">" in raw else raw
        if out["weight_lb"] is None:
            m = _WEIGHT_RE.search(text)
            if m:
                value, unit = float(m.group(1)), m.group(2).lower()
                if unit.startswith("k"):
                    lb = value * _KG_TO_LB
                elif unit.startswith("o"):
                    lb = value / 16.0
                else:
                    lb = value
                if m.group(3):
                    lb += float(m.group(3)) / 16.0
                if lb > 0:
                    out["weight_lb"] = round(lb, 2)
                    out["weight_text"] = m.group(0).strip()
        if out["dims"] is None:
            for m in _DIMS_RE.finditer(text):
                units = [m.group(2), m.group(4), m.group(6)]
                cue = _DIMS_CUE_RE.search(text[max(0, m.start() - 40):m.start()])
                if not any(units) and not cue:
                    continue
                nums = [float(m.group(i)) for i in (1, 3, 5)]
                if any(n <= 0 for n in nums):
                    continue
                tail = re.match(r"\s*([A-Za-z]+)", text[m.end():])
                if tail and not units[2]:
                    # ``400 x 300 x 200 mm`` is handled by the unit group;
                    # what lands here is a unit we do not know (m, ft, Hz).
                    out["warnings"].append(
                        f"Ignored dimensions '{m.group(0).strip()} "
                        f"{tail.group(1)}' — unit '{tail.group(1)}' is not "
                        "in/cm/mm.")
                    continue
                # An axis without its own unit inherits the next explicit one.
                filled, carry = [], None
                for u in reversed(units):
                    carry = u.lower() if u else carry
                    filled.append(carry)
                filled.reverse()
                dims = tuple(round(n / _UNIT_DIVISOR[u[0]], 2) if u else n
                             for n, u in zip(nums, filled))
                out["dims"] = dims
                out["dims_text"] = m.group(0).strip()
                break
    return out


def _pos(v: Any) -> Optional[float]:
    """Positive float or ``None`` (False / 0 / text → None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


#: Template field → report key for the package block.
_PACKAGE_KEYS = {"weight": "weight_lb", "ebay_pkg_length_in": "length_in",
                 "ebay_pkg_width_in": "width_in", "ebay_pkg_height_in": "height_in"}
_DIM_KEYS = ("length_in", "width_in", "height_in")
_DIM_FIELDS = ("ebay_pkg_length_in", "ebay_pkg_width_in", "ebay_pkg_height_in")


def _package_values(rec: dict) -> dict:
    """Weight (lb) and dims (in) as floats or ``None`` from a template read."""
    return {key: _pos(rec.get(field)) for field, key in _PACKAGE_KEYS.items()}


def _box_dims(row: dict) -> Optional[tuple[float, float, float]]:
    """``(L, W, H)`` inches from a ``fb_resolve_box`` / ``boxes`` row, or
    ``None`` for a sizeless box (tubes, custom)."""
    dims = tuple(_pos(row.get(k)) for k in ("length", "width", "height"))
    return dims if all(dims) else None  # type: ignore[return-value]


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


def _strict_bool(value) -> Optional[bool]:
    """True/False for a real boolean or the strings true/false/1/0/yes/no
    (case-insensitive); ``None`` for anything else — ``bool("false")`` is
    True, which must never turn into an attribute-creating write."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no", ""):
            return False
    return None


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
        # Stale-listing digest (sale_ebay >= 1.47.0); each re-validates its
        # verdict and the promotions group server-side before touching eBay.
        "action_ebay_stale_cut",
        "action_ebay_stale_end_and_scrap",
    })
    #: Alias so existing callers of :meth:`run_product_action` keep working.
    ALLOWED_PRODUCT_ACTIONS = ALLOWED_ACTIONS

    def available(self) -> bool:
        """``product.template`` always exists; eBay is available only when
        ``sale_ebay`` has added its fields to it."""
        if self._available is None:
            if super().available() and self._model_field_cache:
                self._available = "ebay_use" in self._model_field_cache
                if not self._available:
                    logger.info("product.template has no ebay_use field "
                                "(sale_ebay not installed)")
        return bool(self._available)

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
        raw = str(ref if ref is not None else "").strip()
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
        m = re.match(r"^sku[:\s]+(\S+)$", raw, re.I)
        token = m.group(1) if m else raw
        explicit_sku = bool(m)
        if _SKU_RE.match(token) and (explicit_sku or "-" in token
                                     or token.upper() == token):
            # =ilike is case-insensitive but treats _ and % as wildcards;
            # escape them so "FBM_1" cannot match "FBM-1".
            escaped = token.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
            rows = self.client.search_read(
                self.MODEL, [["default_code", "=ilike", escaped]],
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
        ``apply_ebay_policies_from_category`` overwrites unconditionally, so
        this reads the category defaults itself and issues ONE write holding
        only the fields that are still blank — a value chosen by hand is
        never touched, and a failure mid-way cannot leave the product
        half-copied. Returns the resulting policy field ids.
        """
        self._require()
        if categ_id:
            self.client.write(self.MODEL, product_tmpl_id, {"categ_id": int(categ_id)})
        current = self.client.read(
            self.MODEL, [product_tmpl_id], fields=["categ_id", *_POLICY_FIELDS])[0]
        result = {f: _m2o_id(current.get(f)) for f in _POLICY_FIELDS}
        categ = _m2o_id(current.get("categ_id"))
        if not categ:
            return result
        try:
            cat_rows = self.client.read(
                "product.category", [categ], fields=list(_POLICY_FIELDS))
        except OdooError:
            # odoo-ebay-custom (which adds these fields to the category)
            # is not installed — nothing to copy.
            return result
        defaults = cat_rows[0] if cat_rows else {}
        fill = {
            f: _m2o_id(defaults.get(f)) for f in _POLICY_FIELDS
            if not result[f] and _m2o_id(defaults.get(f))
        }
        if fill:
            self.client.write(self.MODEL, product_tmpl_id, fill)
            result.update(fill)
        return result

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
        weight_lb: Optional[float] = None,
        dims: Any = None,
        shipping_mode: Optional[str] = None,
        return_mode: Optional[str] = None,
        warranty: Optional[str] = None,
        box: Any = None,
    ) -> dict:
        """Prepare a product for eBay and report readiness. Does NOT publish.

        Steps, each idempotent:

        1. ``ebay_use = True`` on the template and every variant (push
           filters variants on it); ``sale_ok = True`` (Marketplace temp
           items are created unsaleable — eBay orders need a saleable
           product).
        2. Category defaults into blank fields (:meth:`set_category_defaults`,
           optionally re-categorising to *categ_id* first).
        3. Listing defaults: FixedPriceItem / GTC, ``ebay_fixed_price`` from
           ``list_price``, title from name, best offer, stock sync (storable
           products only) with ``ebay_quantity`` from on-hand stock. When
           *fb_listing_id* is given: condition mapped from the FB listing
           (:data:`FB_CONDITION_TO_EBAY`), description (minus its trailing
           "Local pickup…" sentence) and title from it when not supplied.
           Caller's *vals* / *description* last, so they win.
        4. One wizard save with all of that plus *weight_lb* / *dims*
           (``"LxWxH"`` inches or three numbers) — the condition reaches
           Odoo BEFORE the policies are resolved. *box* — a warehouse box
           by id, name or size (``fb_marketplace.boxes``) — supplies the
           dims when *dims* is not given and is written to the product
           (``ebay_package_type_id``) after the save; ``""`` clears it.
        5. Package: the server's package block (legacy attribute fallbacks
           included) is the baseline; for what is still blank
           :func:`_parse_package_text` looks through ``description_sale``,
           ``ebay_description`` and the FB description and a second save
           applies what it finds (noted). Nothing found is a warning, not a
           block (readiness stays Odoo's): ``state["package"]`` keeps the
           server's keys and adds ``weight_lb`` / ``length_in`` / ``width_in``
           / ``height_in`` and ``source`` field / description / missing.
        6. Policy overrides — *shipping_mode* (auto/free/calculated/freight),
           *return_mode* (auto/accept/none), *warranty* (auto/none/factory/
           30d/1y/2y/3y) — are written and ``ebay_apply_resolved_policies``
           resolves shipping / return / warranty from the saved condition +
           category + those modes (odoo-ebay-custom 1.16; skipped with a
           note on an older server). *fallback* — ``{"payment_policy_id",
           "return_policy_id", "shipping_policy_id", "template_id"}`` — fills
           only what is STILL blank afterwards, i.e. a category with no
           policy at all, and says so in ``notes``.
        7. Photos copied from the FB listing to the gallery; the state is
           re-read when anything after the save changed it.

        Returns the wizard state plus ``readiness``, ``package``,
        ``policy_resolution`` (the server's resolver dict, passed through)
        and ``staged`` (what was written). Publishing is :meth:`publish`, a
        separate confirmed step.
        """
        self._require()
        overrides = self._policy_overrides(shipping_mode, return_mode, warranty)
        dims_in = _parse_dims(dims)
        if weight_lb is not None:
            try:
                weight_lb = float(weight_lb)
            except (TypeError, ValueError):
                raise ValueError(f"weight_lb must be a number, got {weight_lb!r}")
            if weight_lb <= 0:
                raise ValueError("weight_lb must be greater than zero")
        box_row = self._resolve_box(box) if box is not None else None
        if box_row and not dims_in:
            dims_in = _box_dims(box_row)
        tmpl = self.client.read(
            self.MODEL, [product_tmpl_id], fields=self._fields(detail=True))
        if not tmpl:
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

        # 2. category defaults into blank fields; the ids are re-read after
        #    the resolver runs (step 6) and the fallback fills what is left.
        policies = self.set_category_defaults(product_tmpl_id, categ_id)
        if categ_id:
            staged["categ_id"] = categ_id

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
            # Mirror what push does (max(virtual_available, 0)) so the
            # readiness check sees the real quantity; a stale default of 1
            # must not publish a positive quantity for an empty shelf.
            on_hand = max(int(tmpl.get("virtual_available") or 0), 0)
            defaults["ebay_quantity"] = on_hand
            if on_hand <= 0:
                notes.append("No stock on hand — quantity set to 0; publish is blocked until stock exists.")
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
        defaults.update(vals or {})
        # Q11/Q13: stock sync is only right when eBay's quantity IS the
        # on-hand quantity. An operator quantity that differs from stock
        # (a partial lot, a reserved unit) would be overwritten by the next
        # sync, so it turns sync off and says so.
        if (vals or {}).get("ebay_quantity") is not None:
            try:
                qty = int(float(vals["ebay_quantity"]))
            except (TypeError, ValueError):
                raise ValueError(
                    f"ebay_quantity must be a whole number, got {vals['ebay_quantity']!r}")
            if qty < 0:
                raise ValueError("ebay_quantity cannot be negative")
            defaults["ebay_quantity"] = qty
            if defaults.get("ebay_sync_stock"):
                on_hand = max(int(tmpl.get("virtual_available") or 0), 0)
                if qty != on_hand:
                    defaults["ebay_sync_stock"] = False
                    notes.append(
                        f"Quantity {qty} differs from stock on hand "
                        f"({on_hand}); eBay stock sync left OFF for this product.")
        if description is None and fb.get("description"):
            description = _strip_fb_pickup(fb["description"])
            if description != fb["description"]:
                notes.append("Dropped the FB copy's 'Local pickup' sentence from the eBay description.")

        # 4. save — condition, defaults, caller's vals and package values go
        #    to Odoo BEFORE the policy resolver so it resolves from the final
        #    condition (the wizard save re-applies the resolution itself on
        #    a condition/category change; step 6 covers the overrides).
        caller_pkg = self._caller_package(weight_lb, dims_in, vals)
        defaults.update(caller_pkg)
        state = self.set_listing_fields(product_tmpl_id, defaults, description)
        staged.update(defaults)
        if description is not None:
            staged["ebay_description"] = True
        if box is not None:
            staged["ebay_package_type_id"] = self._write_box(
                product_tmpl_id, box_row, notes, caller_pkg)

        # 5. package: the server's canonical package (legacy attribute
        #    fallbacks included) is the baseline; only what is still blank
        #    comes from the descriptions.
        package, pkg_fill = self._stage_package(
            state.get("package"), tmpl, fb, caller_pkg, notes)
        if pkg_fill:
            state = self.set_listing_fields(product_tmpl_id, pkg_fill)
            staged.update(pkg_fill)

        # 6. policies: overrides → resolver → fallback for what is still blank
        stale = False
        if overrides:
            self.client.write(self.MODEL, product_tmpl_id, overrides)
            staged.update(overrides)
            stale = True
        if self._apply_resolved_policies(product_tmpl_id, notes):
            staged["policies_resolved"] = True
            stale = True
            current = self.client.read(
                self.MODEL, [product_tmpl_id], fields=list(_POLICY_FIELDS))
            if current:
                policies = {f: _m2o_id(current[0].get(f)) for f in _POLICY_FIELDS}
        fill: dict[str, Any] = {}
        for key, field in _FALLBACK_KEYS.items():
            if not policies.get(field) and (fallback or {}).get(key):
                fill[field] = int(fallback[key])
        if fill:
            notes.append("Fallback policies used for: " + ", ".join(sorted(fill))
                         + " (the product category has no default for them).")
            state = self.set_listing_fields(product_tmpl_id, fill)
            staged.update(fill)
            stale = False

        # 7. photos
        if fb_listing_id and fb:
            copied = self.add_images_from_fb(product_tmpl_id, fb_listing_id)
            notes.append(copied["summary"])
            if copied["copied"]:
                stale = True
        if stale:
            state = self.listing_state(product_tmpl_id)

        state["staged"] = staged
        state["notes"] = notes
        # Server package dict (weight_lb/length/width/height/package_type)
        # stays the baseline; the ``*_in`` keys and sources sit on top.
        state["package"] = {**(state.get("package") or {}), **package}
        # odoo-ebay-custom 1.16 adds the resolver dict to the wizard state;
        # an older server has no key — keep the shape stable for callers.
        state["policy_resolution"] = state.get("policy_resolution") or {}
        state["summary"] = (
            f"Staged product {product_tmpl_id} '{tmpl.get('name')}': "
            + ("ready to publish." if (state.get("readiness") or {}).get("can_push")
               else "blocked — " + "; ".join((state.get("readiness") or {}).get("blockers") or []))
        )
        return state

    def _resolve_box(self, box: Any) -> Optional[dict]:
        """Warehouse box → ``{"id", "name", "length", "width", "height"}``
        (inches, 0 when the box has no size) through fb_marketplace_lister
        4.4's read-only ``fb_resolve_box``; ``None`` when *box* clears.
        Unknown / ambiguous boxes are the server's error, naming the
        candidates; an older server gets a plain "not available" error."""
        arg = _box_arg(box)
        if arg is False:
            return None
        try:
            row = self.client.execute(self.MODEL, "fb_resolve_box", arg)
        except OdooError as exc:
            if server_lacks_method(exc, "fb_resolve_box"):
                raise OdooError("Warehouse boxes need fb_marketplace_lister >= 4.4 "
                                "on this server; pass dims instead.") from exc
            raise
        if not isinstance(row, dict) or not row.get("id"):
            raise OdooError(f"Server returned no box for {box!r}.")
        return dict(row)

    def _write_box(self, product_tmpl_id: int, box_row: Optional[dict],
                   notes: list[str], saved: Optional[dict] = None) -> int | bool:
        """Write ``ebay_package_type_id`` and note it. The dims already went
        through the wizard save (revise diff); they are sent again in the
        same write because sale_ebay autofills the box's size on a box-only
        write, which would clobber an explicit ``dims`` / ``vals`` size."""
        box_id = int(box_row["id"]) if box_row else False
        write_vals: dict[str, Any] = {"ebay_package_type_id": box_id}
        if box_id:
            write_vals.update({f: saved[f] for f in _DIM_FIELDS if (saved or {}).get(f)})
        self.client.write(self.MODEL, product_tmpl_id, write_vals)
        if box_row:
            dims = _box_dims(box_row)
            size = f" ({dims[0]:g}×{dims[1]:g}×{dims[2]:g} in)" if dims else " (no size)"
            notes.append(f"Box #{box_row['id']} {box_row.get('name')}{size}.")
        else:
            notes.append("Warehouse box cleared.")
        return box_id

    @staticmethod
    def _policy_overrides(shipping_mode: Optional[str], return_mode: Optional[str],
                          warranty: Optional[str]) -> dict:
        """Validate the override arguments → ``ebay_*`` field values.

        Refuses before any write: a typo must not stage half a listing.
        """
        out: dict[str, Any] = {}
        if shipping_mode is not None:
            mode = str(shipping_mode).strip().lower()
            if mode not in SHIPPING_MODES:
                raise ValueError(
                    f"shipping_mode must be one of {', '.join(SHIPPING_MODES)}, got {shipping_mode!r}")
            out["ebay_shipping_mode"] = mode
        if return_mode is not None:
            mode = str(return_mode).strip().lower()
            if mode not in RETURN_MODES:
                raise ValueError(
                    f"return_mode must be one of {', '.join(RETURN_MODES)}, got {return_mode!r}")
            out["ebay_return_mode"] = mode
        if warranty is not None:
            key = str(warranty).strip().lower()
            if key not in WARRANTY_CHOICES:
                raise ValueError(
                    f"warranty must be one of {', '.join(WARRANTY_CHOICES)}, got {warranty!r}")
            kind, months = WARRANTY_CHOICES[key]
            out["ebay_warranty_kind"] = kind
            out["ebay_warranty_months"] = months
        return out

    def _apply_resolved_policies(self, product_tmpl_id: int, notes: list[str]) -> bool:
        """``ebay_apply_resolved_policies`` (odoo-ebay-custom 1.16): writes
        shipping / return policy ids and the Warranty item specific from
        condition, category and the ``ebay_*_mode`` overrides. On an
        ``ebay_policy_manual`` product only the policy ids are frozen; the
        Warranty specific is still (re)applied. ``False`` (with a note)
        when the server does not have the method; the blank-only category
        copy then stands, as before."""
        try:
            self.client.execute(self.MODEL, "ebay_apply_resolved_policies", [product_tmpl_id])
        except OdooError as exc:
            # a missing method only; a resolver that raised still surfaces
            if server_lacks_method(exc, "ebay_apply_resolved_policies"):
                notes.append("Policy resolver not available on this server "
                             "(odoo-ebay-custom < 1.16); category defaults used as-is.")
                return False
            raise
        return True

    @staticmethod
    def _caller_package(weight_lb: Optional[float], dims_in: Optional[tuple],
                        vals: Optional[dict]) -> dict:
        """Package field values the caller asked for — *weight_lb* / *dims*
        first, then any ``weight`` / ``ebay_pkg_*_in`` in *vals* on top (vals
        are last, so they win). Non-positive vals entries are ignored."""
        out: dict[str, Any] = {}
        if weight_lb:
            out["weight"] = float(weight_lb)
        if dims_in:
            out["ebay_pkg_length_in"], out["ebay_pkg_width_in"], \
                out["ebay_pkg_height_in"] = dims_in
        for field in _PACKAGE_FIELDS:
            v = (vals or {}).get(field)
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                out[field] = f
        return out

    def _stage_package(self, server_pkg: Optional[dict], tmpl: dict, fb: dict,
                       caller_pkg: dict, notes: list[str]) -> tuple[dict, dict]:
        """Effective package after the save and what is still to fill.

        Precedence: caller's values (already saved, overlaid again in case
        the server did not echo them) > the server's package dict from the
        wizard state (legacy attribute fallbacks included; a template read
        when the server has no package block) > the product / eBay / FB
        description text, which fills only the fields that are still blank.
        What is still missing is a warning (D1), never a block — Kevin
        re-stages with the values or asks.

        Returns ``(package, fill)``: the report dict (``weight_lb``,
        ``length_in``, ``width_in``, ``height_in``, ``source``,
        ``weight_source``, ``dims_source``) and the field values to save.
        """
        if server_pkg:
            have = {
                "weight_lb": _pos(server_pkg.get("weight_lb")),
                "length_in": _pos(server_pkg.get("length")),
                "width_in": _pos(server_pkg.get("width")),
                "height_in": _pos(server_pkg.get("height")),
            }
        else:
            have = _package_values(tmpl)
        for field, key in _PACKAGE_KEYS.items():
            if caller_pkg.get(field):
                have[key] = float(caller_pkg[field])
        weight_src = "field" if have["weight_lb"] else "missing"
        dims_src = ("field" if all(have[k] for k in _DIM_KEYS) else "missing")
        fill: dict[str, Any] = {}
        if weight_src == "missing" or dims_src == "missing":
            found = _parse_package_text([
                tmpl.get("description_sale") or "",
                tmpl.get("ebay_description") or "",
                (fb or {}).get("description") or "",
            ])
            notes.extend(found["warnings"])
            if weight_src == "missing" and found["weight_lb"]:
                fill["weight"] = found["weight_lb"]
                have["weight_lb"] = found["weight_lb"]
                weight_src = "description"
                notes.append(f"Package weight {found['weight_lb']:g} lb taken from the "
                             f"description ('{found['weight_text']}').")
            if dims_src == "missing" and found["dims"]:
                taken = []
                for key, field, value in zip(_DIM_KEYS, _DIM_FIELDS, found["dims"]):
                    if not have[key]:
                        fill[field] = value
                        have[key] = value
                        taken.append(f"{key[:-3]} {value:g}")
                dims_src = "description"
                notes.append("Package " + ", ".join(taken) + " in taken from the "
                             f"description ('{found['dims_text']}').")
        missing = [name for name, src in (("weight", weight_src), ("dims", dims_src))
                   if src == "missing"]
        if missing:
            notes.append("Package " + "/".join(missing) + " missing — re-stage with "
                         "--weight <lb> --dims LxWxH (inches), or ask for them; "
                         "never guess.")
            source = "missing"
        elif "description" in (weight_src, dims_src):
            source = "description"
        else:
            source = "field"
        package = {**have, "source": source, "weight_source": weight_src,
                   "dims_source": dims_src}
        return package, fill

    # ── Publish / end ────────────────────────────────────────────────

    def publish(self, product_tmpl_id: int) -> dict:
        """Publish the product to eBay (live API call, publicly visible).

        Runs the wizard's readiness check first and refuses on any blocker
        or when the product is already live (``ebay_wizard_push`` repeats
        that live check server-side; Kevin is a single worker, so the two
        checks are not otherwise serialised), then calls
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

    # ── Revising a live listing (stage → approve → push) ─────────────

    @staticmethod
    def _revise_result(result, key: str = "success") -> dict:
        if not isinstance(result, dict):
            result = {key: bool(result)}
        return result

    def revision_status(self, product_tmpl_id: int) -> dict:
        """Pending (unpushed) revision diff for a live listing, if any.

        Wraps ``ebay_wizard_revise_status``: ``diff`` (old → new per field
        with a ``pushable`` flag), ``warnings`` (drift since staging, not
        live), ``can_revise`` and the ``hash`` to pass to :meth:`revise`.
        """
        self._require()
        return self._revise_result(self.client.execute(
            self.MODEL, "ebay_wizard_revise_status", [product_tmpl_id]))

    def revise_stage(self, product_tmpl_id: int, vals: Optional[dict] = None,
                     description: Optional[str] = None,
                     refresh_description: bool = False,
                     weight_lb: Optional[float] = None, dims: Any = None,
                     box: Any = None) -> dict:
        """Stage a change to a LIVE listing — writes Odoo, never eBay.

        ``vals`` keys: ``ebay_title``, ``ebay_item_condition_id``,
        ``ebay_condition_description``, ``ebay_fixed_price``,
        ``ebay_quantity``, ``ebay_best_offer*``, ``ebay_template_id``,
        ``weight`` and ``ebay_pkg_*_in`` (category / type / policies are
        not revisable: end + relist). ``weight_lb`` / ``dims`` (``"LxWxH"``
        inches) are shorthand for those package keys (sale_ebay 1.40);
        ``box`` (warehouse box id / name / size, ``fb_marketplace.boxes``)
        supplies the dims when ``dims`` is not given — through the wizard
        save, so the diff shows the size change — and the box itself is
        written to the product afterwards (``""`` clears it).
        ``description`` (text or HTML) goes to ``ebay_description``. Returns
        the server's cumulative diff since the first stage, per-field
        pushability warnings, ``can_revise`` and the ``hash`` that
        :meth:`revise` must echo back. Re-runnable; a quantity change turns
        stock sync off server-side (reported as a warning).

        ``refresh_description=True`` re-sends the body rendered from the
        current description template even when nothing else changed (a
        template edited before the first stage is otherwise invisible on
        listings published before sale_ebay 1.39); the diff then carries a
        ``description`` entry ("as published" → rendered) whose
        pushability tells whether eBay will receive it.
        """
        self._require()
        clean = dict(vals or {})
        if clean.get("ebay_title"):
            clean["ebay_title"] = str(clean["ebay_title"]).strip()[:EBAY_TITLE_MAX]
        dims_in = _parse_dims(dims)
        if weight_lb is not None:
            try:
                clean["weight"] = float(weight_lb)
            except (TypeError, ValueError):
                raise ValueError(f"weight_lb must be a number, got {weight_lb!r}")
            if clean["weight"] <= 0:
                raise ValueError("weight_lb must be greater than zero")
        box_row = self._resolve_box(box) if box is not None else None
        if box_row and not dims_in:
            dims_in = _box_dims(box_row)
        if dims_in:  # explicit vals win over dims / the box's size
            for field, value in zip(_DIM_FIELDS, dims_in):
                clean.setdefault(field, value)
        body = None
        if description is not None:
            body = description if "<" in description and ">" in description \
                else _text_to_html(description)
        args = [[product_tmpl_id], clean, body]
        if refresh_description:
            args.append(True)  # sale_ebay >= 1.39.0 only; older servers reject the arg
        result = self._revise_result(self.client.execute(
            self.MODEL, "ebay_wizard_revise_stage", *args))
        if result.get("success") and box is not None:
            notes: list[str] = []
            result["box_id"] = self._write_box(product_tmpl_id, box_row, notes, clean)
            result.setdefault("warnings", []).extend(notes)
        if result.get("success"):
            result["summary"] = self._revise_summary(result)
        else:
            result["summary"] = result.get("message") or "Stage refused."
        return result

    @staticmethod
    def _revise_summary(result: dict) -> str:
        diff = result.get("diff") or []
        if not diff:
            return "Nothing changed."
        parts = []
        for d in diff:
            if d.get("pushable"):
                mark = ""
            elif d.get("reason"):
                mark = f" (will NOT reach eBay: {d['reason']})"
            else:
                mark = " (Odoo only)"
            parts.append(f"{d.get('label')}: {d.get('old')!r} → {d.get('new')!r}{mark}")
        head = "Revise ready" if result.get("can_revise") else "Nothing pushable"
        return f"{head}: " + "; ".join(parts)

    def revise(self, product_tmpl_id: int, expected_hash: str) -> dict:
        """Push the staged revision to eBay (live API call, public).

        ``expected_hash`` is the ``hash`` from the diff the operator
        approved; the server refuses (``stale``) when the product changed
        since, and also on ``nothing_staged`` / ``nothing_to_push`` /
        ``no_offer`` / ``not_live`` / ``busy``. Returns ``{"revised",
        "reason"?, "ebay_url", "status", "summary", "pushed", "skipped"}``;
        never raises for a refusal so the caller can relay it verbatim. An
        ``ebay_error`` carries ``partial_risk``: check the live listing
        before retrying.
        """
        self._require()
        if not expected_hash:
            return {"revised": False, "reason": "hash_required",
                    "summary": "Pass the hash from the staged diff."}
        result = self._revise_result(self.client.execute(
            self.MODEL, "ebay_wizard_revise", [product_tmpl_id], str(expected_hash)))
        if not result.get("success"):
            return {"revised": False,
                    "reason": result.get("error") or "revise_failed",
                    "partial_risk": bool(result.get("partial_risk")),
                    "ebay_url": result.get("ebay_url") or None,
                    "summary": result.get("message") or "eBay revise failed."}
        return {
            "revised": True,
            "ebay_url": result.get("ebay_url") or None,
            "status": result.get("status"),
            "pushed": result.get("pushed") or [],
            "skipped": result.get("skipped") or [],
            "summary": result.get("message") or "Revised on eBay.",
        }

    def revise_discard(self, product_tmpl_id: int) -> dict:
        """Drop the staged revision: restore the snapshot in Odoo (eBay is
        untouched). ``not_restored`` lists what could not be put back
        (deleted photos, item specifics)."""
        self._require()
        result = self._revise_result(self.client.execute(
            self.MODEL, "ebay_wizard_revise_discard", [product_tmpl_id]))
        if not result.get("success"):
            result["summary"] = result.get("message") or "Nothing to discard."
            return result
        restored = ", ".join(result.get("restored") or []) or "nothing"
        result["summary"] = f"Discarded staged revision ({restored} restored)."
        return result

    # ── Item specifics (eBay category aspects) ───────────────────────
    #
    # eBay refuses publishOffer when a REQUIRED aspect of the category is
    # missing ("The item specific Type is missing"). Specifics are the
    # product's attribute lines; the server (sale_ebay ≥ 1.38) knows which
    # aspects the category requires and validates against eBay's allowed
    # values. Fill them at stage time; ``publish`` refuses with
    # ``missing_specifics`` while required ones are unfilled.

    @staticmethod
    def _specifics_entry_text(e: dict) -> str:
        status = e.get("status")
        name = e.get("name")
        if status == "ok":
            return f"{name}={e.get('value')}"
        if status == "multi_value":
            return (f"{name}={e.get('value')} — MULTI-VALUE (eBay takes one; "
                    "keep the value the item actually has, or ask the operator)")
        if status == "invalid":
            sample = ", ".join(e.get("values_sample") or [])
            more = e.get("values_total", 0) - len(e.get("values_sample") or [])
            hint = f" (allowed: {sample}{f' …+{more}' if more > 0 else ''})" if sample else ""
            return f"{name}={e.get('value')} — INVALID{hint}"
        if status == "variant_attribute":
            return f"{name} — VARIANT ATTRIBUTE (not fillable here; set in Odoo)"
        label = "PLACEHOLDER" if status == "placeholder" else "MISSING"
        if e.get("mode") == "select" and e.get("values_sample"):
            sample = ", ".join(e["values_sample"])
            more = e.get("values_total", 0) - len(e["values_sample"])
            return f"{name} — {label} (select: {sample}{f' …+{more}' if more > 0 else ''})"
        return f"{name} — {label}" + ("" if e.get("attribute_id") else " (no Odoo attribute)")

    def _specifics_summary(self, st: dict) -> str:
        if not st.get("success"):
            return st.get("message") or f"Refused: {st.get('error') or 'unknown'}"
        req = "; ".join(self._specifics_entry_text(e) for e in st.get("required") or []) or "none"
        opt = "; ".join(self._specifics_entry_text(e) for e in st.get("optional") or [])
        parts = [f"required: {req}"]
        if opt:
            parts.append(f"optional: {opt}")
        if st.get("extra"):
            parts.append("also sent: " + ", ".join(st["extra"]))
        if not st.get("can_push"):
            head = "BLOCKED — eBay requires: " + ", ".join(st.get("blocking") or [])
        elif st.get("source") == "none":
            head = ("Specifics UNVERIFIED — eBay aspect list unavailable, requirements "
                    "unknown (retry with refresh before publishing)")
        else:
            head = "Specifics OK"
        return head + " | " + " | ".join(parts)

    def specifics_status(self, product_tmpl_id: int, refresh: bool = False) -> dict:
        """Required / optional item specifics of the product's eBay category
        vs. its attribute lines (``ebay_wizard_specifics_status``).

        Entries carry ``status`` ``ok`` | ``missing`` | ``placeholder`` |
        ``invalid`` | ``multi_value`` | ``variant_attribute``, the stored
        ``value``, ``attribute_id`` (an Odoo attribute exists), ``mode``
        (``select`` = eBay's list is exhaustive, see ``values_sample`` /
        :meth:`category_aspects`). ``blocking`` names stop ``publish``.
        ``source`` ``none`` = eBay unreachable with no cached list.
        """
        self._require()
        st = self._revise_result(self.client.execute(
            self.MODEL, "ebay_wizard_specifics_status", [product_tmpl_id], bool(refresh)))
        st["summary"] = self._specifics_summary(st)
        return st

    def category_aspects(self, product_tmpl_id: int, refresh: bool = False,
                         aspect: Optional[str] = None) -> dict:
        """eBay's aspect list for the product's category (cached 7 days on
        the category; ``refresh`` re-fetches). ``aspect`` narrows to one
        name (case-insensitive) with its full allowed-value list."""
        self._require()
        out = self._revise_result(self.client.execute(
            self.MODEL, "ebay_wizard_category_aspects", [product_tmpl_id], bool(refresh)))
        aspects = out.get("aspects") or []
        if aspect:
            key = " ".join(aspect.split()).lower()
            aspects = [a for a in aspects if " ".join(a["name"].split()).lower() == key]
            out["aspects"] = aspects
        if not out.get("success"):
            out["summary"] = out.get("message") or "No aspect data."
        elif aspect and not aspects:
            out["summary"] = f"No aspect named {aspect!r} in this category."
        else:
            bits = []
            for a in aspects:
                flag = "REQUIRED" if a.get("required") else a.get("usage", "optional")
                if a.get("mode") == "select":
                    vals = a.get("values") or []
                    shown = vals if aspect else vals[:8]
                    tail = "" if aspect or len(vals) <= 8 else f" …+{len(vals) - 8}"
                    bits.append(f"{a['name']} [{flag}, select: {', '.join(shown)}{tail}]")
                else:
                    bits.append(f"{a['name']} [{flag}, free text]")
            out["summary"] = "; ".join(bits) or "No aspects."
        return out

    def set_specifics(self, product_tmpl_id: int, values: dict,
                      create_attributes: bool = False, dry_run: bool = False) -> dict:
        """Fill item specifics by aspect name (``{"Type": "Headset"}``).

        Server rules (``ebay_wizard_set_specifics``): only aspects of the
        product's eBay category or lines the product already has; values on
        existing attributes are matched case-insensitively and created when
        absent; select-only aspects are normalised to eBay's spelling or
        refused (``invalid_value`` + ``suggestions``); a MISSING Odoo
        attribute is only created with ``create_attributes=True`` — the
        operator must OK that first (``needs_attributes`` lists them);
        variant-creating attributes are never written. ``dry_run`` reports
        without writing. Returns per-aspect ``results`` plus the fresh
        ``status`` (not on dry run). Values must come from the item itself
        (listing text / photos) — never guessed to satisfy a requirement.
        """
        self._require()
        if not isinstance(values, dict) or not values:
            return {"success": False, "error": "bad_values",
                    "summary": "Pass a non-empty mapping of aspect name → value."}
        clean = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in values.items()}
        create = _strict_bool(create_attributes)
        if create is None:
            return {"success": False, "error": "bad_create_attributes",
                    "summary": "create_attributes must be true or false "
                               f"(got {create_attributes!r}); it needs the operator's explicit OK."}
        out = self._revise_result(self.client.execute(
            self.MODEL, "ebay_wizard_set_specifics", [product_tmpl_id], clean,
            create, bool(dry_run)))
        if not out.get("success"):
            out["summary"] = out.get("message") or f"Refused: {out.get('error')}"
            return out
        lines = []
        for r in out.get("results") or []:
            name, sk = r.get("name"), r.get("skipped")
            if sk == "attribute_missing":
                lines.append(f"{name} — NO ODOO ATTRIBUTE (ask Ian; retry with create_attributes)")
            elif sk == "invalid_value":
                sug = ", ".join(r.get("suggestions") or [])
                lines.append(f"{name}={r.get('input')!r} — INVALID" + (f" (did you mean: {sug})" if sug else ""))
            elif sk == "variant_attribute":
                lines.append(f"{name} — skipped (variant attribute)")
            elif sk == "not_an_aspect":
                lines.append(f"{name} — skipped (not an aspect of this category)")
            elif sk == "aspects_unavailable":
                lines.append(f"{name} — skipped (eBay aspect list unavailable, retry later)")
            elif sk:
                lines.append(f"{name} — skipped ({sk})")
            else:
                tags = []
                if r.get("attribute") == "created":
                    tags.append("new attribute")
                if r.get("value") == "created":
                    tags.append("new value")
                tag = f" ({', '.join(tags)})" if tags else ""
                verb = "would write" if dry_run else "wrote"
                lines.append(f"{name}={r.get('stored_value')}{tag} [{verb}]")
        out["summary"] = "; ".join(lines) or "Nothing to write."
        if out.get("needs_attributes"):
            out["summary"] += " | needs OK to create attribute(s): " + ", ".join(out["needs_attributes"])
        if out.get("status"):
            out["summary"] += " | " + self._specifics_summary(out["status"])
        return out

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
                       listings: Optional[list[dict]] = None,
                       stamp: bool = True) -> dict:
        """Store externally gathered comps (e.g. sold prices read from eBay in
        a browser) as the product's comp aggregates.

        The Browse API only sees *asking* prices; sold/completed prices need a
        browser. Writes ``ebay_comp_low/p25/median/high/count/fetched_at`` and
        a JSON note of the raw prices; ``ebay_suggested_price`` and
        ``ebay_comp_note`` are Odoo-computed from these, so the cost floor
        and anchor rules still apply. Refuses on fewer than 3 prices — call
        :meth:`record_no_comps` for that case so the stale digest can tell
        "no sold comps" from "never researched".

        With *stamp* (default) and a server that has the stale digest, the
        sold-check stamp (``ebay_sold_checked_at``) is written right after
        the aggregates; the server refuses the stamp unless the JSON source
        is ``ebay_sold_browser``.
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
        stamped = False
        if stamp and source == "ebay_sold_browser" and self._has_stale_digest():
            self.client.execute(self.MODEL, "ebay_stale_mark_sold_checked", [product_tmpl_id])
            stamped = True
        pricing = self.get_pricing(product_tmpl_id)
        pricing["written"] = True
        pricing["stamped"] = stamped
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

    # ── Stale-listing digest (sale_ebay >= 1.47.0) ───────────────────
    #
    # The server owns the rules (age buckets, verdicts, cost floor, the
    # 25% cut ceiling, scrap safety). These wrappers only add dry runs,
    # summaries and the ``confirm`` gate; nothing here decides a price.

    def _has_stale_digest(self) -> bool:
        self._require()
        return "ebay_stale_verdict" in (self._model_field_cache or set())

    def _require_stale_digest(self) -> None:
        if not self._has_stale_digest():
            raise OdooError("The stale-listing digest needs sale_ebay >= 1.47.0 "
                            "(product.template has no ebay_stale_verdict).")

    @staticmethod
    def _stale_row_text(row: dict) -> str:
        """One digest line for a review row."""
        name = (row.get("name") or "")[:40]
        price = row.get("price") or 0.0
        v = row.get("verdict")
        if v == "cut":
            return (f"#{row['id']} {name} ${price:.0f} → ${row.get('suggested') or 0:.0f} "
                    f"(−{row.get('discount_pct') or 0:.1f}%, med ${row.get('comp_median') or 0:.0f} "
                    f"n={row.get('comp_count') or 0})")
        if v == "promo":
            return (f"#{row['id']} {name} ${price:.0f} · med ${row.get('comp_median') or 0:.0f} "
                    f"(n={row.get('comp_count') or 0}) → {row.get('promo_pct') or 0}% markdown")
        if v == "end":
            return (f"#{row['id']} {name} ${price:.0f} · no sold comps · "
                    f"on hand {row.get('qty_on_hand') or 0:g}")
        return f"#{row['id']} {name} ${price:.0f} · {row.get('note') or v}"

    def stale_review(self, limit: Optional[int] = None,
                     buckets: Optional[list[str]] = None) -> dict:
        """Age-bucketed review of unsold live listings with server verdicts.

        Returns the server payload (``counts``, ``buckets``,
        ``needs_research``) plus per-row ``text`` and a ``summary``.

        Classified as a read (no ``--confirm``) on purpose: the only thing
        the server touches is ``ebay_days_listed``, a stored field derived
        from ``ebay_start_date`` and today's date, which it recomputes so
        the buckets track the calendar (the same recompute the daily
        ``sale_ebay`` cron does). No listing, price or stock data changes.
        """
        self._require_stale_digest()
        kwargs: dict[str, Any] = {}
        if limit:
            kwargs["limit"] = int(limit)
        if buckets:
            kwargs["buckets"] = list(buckets)
        res = self.client.execute(self.MODEL, "ebay_stale_review", **kwargs) or {}
        for rows in (res.get("buckets") or {}).values():
            for row in rows:
                row["text"] = self._stale_row_text(row)
        c = res.get("counts") or {}
        bv = c.get("by_verdict") or {}
        res["summary"] = (
            f"Live {c.get('live', 0)} · unsold {c.get('unsold', 0)} · "
            f"needs research {c.get('needs_research', 0)} · "
            f"promo {bv.get('promo', 0)} · cut {bv.get('cut', 0)} · "
            f"end {bv.get('end', 0)} · hold {bv.get('hold', 0) + bv.get('nocomps', 0)}")
        return res

    def stale_needs_research(self, limit: Optional[int] = None,
                             buckets: Optional[list[str]] = None) -> list[int]:
        """Product ids that still need a sold-comps check, oldest first
        (never researched, then re-check TTL expired / Browse-overwritten)."""
        ids = list(self.stale_review(buckets=buckets).get("needs_research") or [])
        return ids[:int(limit)] if limit else ids

    def record_no_comps(self, product_tmpl_id: int) -> dict:
        """A sold check ran and found < 3 comparable sales: zero the
        aggregates and stamp the sold check so the verdict becomes
        ``nocomps`` / ``end`` instead of "not researched"."""
        self._require_stale_digest()
        self.client.execute(self.MODEL, "ebay_stale_record_no_comps", [product_tmpl_id])
        row = self.client.read(self.MODEL, [product_tmpl_id],
                               fields=["name", "ebay_stale_verdict", "ebay_stale_verdict_note"])
        rec = row[0] if row else {}
        return {"recorded": True, "verdict": rec.get("ebay_stale_verdict"),
                "summary": f"{rec.get('name')}: no sold comps recorded → "
                           f"{rec.get('ebay_stale_verdict')} ({rec.get('ebay_stale_verdict_note')})"}

    def mark_sold_checked(self, product_tmpl_id: int) -> dict:
        """Stamp ``ebay_sold_checked_at`` on comps already written with
        source ``ebay_sold_browser`` (the server refuses anything else)."""
        self._require_stale_digest()
        self.client.execute(self.MODEL, "ebay_stale_mark_sold_checked", [product_tmpl_id])
        return {"stamped": True, "product_tmpl_id": product_tmpl_id}

    def _stale_current(self, product_tmpl_id: int) -> dict:
        rows = self.client.read(self.MODEL, [product_tmpl_id], fields=[
            "name", "ebay_fixed_price", "ebay_suggested_price",
            "ebay_suggested_discount_pct", "ebay_comp_median", "ebay_comp_count",
            "ebay_days_listed", "ebay_age_bucket", "ebay_stale_verdict",
            "ebay_stale_verdict_note", "ebay_stale_promo_pct", "ebay_listing_status"])
        if not rows:
            raise OdooError(f"product.template {product_tmpl_id} not found")
        return rows[0]

    def apply_stale_cut(self, product_tmpl_id: int, max_discount_pct: float = 25.0,
                        confirm: bool = False) -> dict:
        """Push the server's suggested price to eBay for a ``cut`` verdict.

        Dry run unless ``confirm=True``. Unlike :meth:`apply_suggested_price`
        (local write only) this reaches eBay via ``action_ebay_stale_cut``,
        which re-checks the verdict, the ceiling and the promotions group.
        """
        self._require_stale_digest()
        rec = self._stale_current(product_tmpl_id)
        cur, new = rec.get("ebay_fixed_price") or 0.0, rec.get("ebay_suggested_price") or 0.0
        pct = rec.get("ebay_suggested_discount_pct") or 0.0
        if rec.get("ebay_stale_verdict") != "cut":
            return {"applied": False, "record": rec,
                    "summary": f"Refused: {rec.get('name')} is not a cut candidate "
                               f"({rec.get('ebay_stale_verdict_note') or rec.get('ebay_stale_verdict')})."}
        if pct > float(max_discount_pct):
            return {"applied": False, "record": rec,
                    "summary": f"Refused: suggested cut of {pct:.1f}% exceeds the "
                               f"{float(max_discount_pct):.1f}% ceiling. Raise max_discount_pct to override."}
        if not confirm:
            return {"applied": False, "record": rec,
                    "summary": f"Dry run — would cut {rec.get('name')} {cur:.2f} → {new:.2f} "
                               f"(−{pct:.1f}%) on eBay. Pass confirm=True to apply."}
        result = self.client.execute(self.MODEL, "action_ebay_stale_cut", [product_tmpl_id],
                                     max_discount_pct=float(max_discount_pct)) or {}
        return {"applied": True, "result": result,
                "summary": f"Cut applied on eBay: {rec.get('name')} "
                           f"{result.get('old_price', cur):.2f} → {result.get('new_price', new):.2f} "
                           f"(−{result.get('discount_pct', pct):.1f}%)"}

    def stale_end_preview(self, product_tmpl_id: int) -> dict:
        """What :meth:`end_stale_scrap` would do: listing state and the
        on-hand units (per location / lot) that would be scrapped."""
        self._require_stale_digest()
        prev = self.client.execute(self.MODEL, "ebay_stale_end_preview", [product_tmpl_id]) or {}
        rec = self._stale_current(product_tmpl_id)
        lots = ", ".join(
            f"{s.get('qty'):g}@{s.get('location')}" + (f" lot {s['lot']}" if s.get("lot") else "")
            for s in prev.get("scrap") or []) or "nothing on hand"
        prev["record"] = rec
        prev["summary"] = (f"{rec.get('name')}: listing {prev.get('listing_status')}, "
                           f"would scrap {prev.get('total_qty', 0):g} unit(s): {lots}")
        return prev

    def end_stale_scrap(self, product_tmpl_id: int, confirm: bool = False) -> dict:
        """End the listing, scrap every on-hand unit and archive the product
        (``end`` verdict only). Dry run unless ``confirm=True``."""
        self._require_stale_digest()
        prev = self.stale_end_preview(product_tmpl_id)
        rec = prev["record"]
        if rec.get("ebay_stale_verdict") != "end":
            return {"ended": False, "preview": prev,
                    "summary": f"Refused: {rec.get('name')} is not an end candidate "
                               f"({rec.get('ebay_stale_verdict_note') or rec.get('ebay_stale_verdict')})."}
        if not confirm:
            return {"ended": False, "preview": prev,
                    "summary": f"Dry run — {prev['summary']}. Pass confirm=True to end + scrap + archive."}
        result = self.client.execute(self.MODEL, "action_ebay_stale_end_and_scrap",
                                     [product_tmpl_id]) or {}
        n = sum(float(s.get("qty") or 0) for s in result.get("scrapped") or [])
        return {"ended": bool(result.get("ended")), "result": result, "preview": prev,
                "summary": f"{rec.get('name')}: listing "
                           f"{'ended' if result.get('ended') else 'was not live'}, "
                           f"scrapped {n:g} unit(s)"
                           f"{', product archived' if result.get('archived') else ''}."}

    def create_stale_promo(self, product_ids: list[int], pct: Optional[int] = None) -> dict:
        """Draft ONE markdown promotion for a batch of 30-day ``promo``
        verdicts (server picks the shallowest floor-safe pct when *pct* is
        omitted). Pushing stays behind ``ebay_promo`` approve."""
        self._require_stale_digest()
        ids = [int(i) for i in (product_ids or [])]
        if not ids:
            raise OdooError("create_stale_promo needs at least one product id")
        kwargs = {"pct": int(pct)} if pct is not None else {}
        res = self.client.execute(self.MODEL, "action_ebay_stale_promo", ids, **kwargs) or {}
        res["summary"] = (
            f"{'Existing' if res.get('existing') else 'Drafted'} promo #{res.get('promotion_id')} "
            f"'{res.get('name')}' at {res.get('pct')}% for {len(res.get('product_ids') or ids)} "
            f"product(s) — not on eBay until `approve promo {res.get('promotion_id')}`.")
        return res

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
