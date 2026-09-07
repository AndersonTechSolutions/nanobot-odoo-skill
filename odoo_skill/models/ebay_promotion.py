"""
eBay promotions (Marketing API) for ``sale_ebay`` — ``ebay.promotion``.

Odoo owns the promotion record and the eBay call; this class only shapes
what an agent can do with it. Two rules matter for unattended use:

* **Drafts are cheap, pushes are gated.** :meth:`create_promotion` and
  :meth:`update_promotion` write Odoo rows only (``status == 'draft'``).
  Nothing reaches eBay until :meth:`approve_promotion`, which the operator
  triggers with a literal ``approve promo <id>``. Pause / resume / end are
  likewise separate writes.

* **Volume tiers on a product are not a promotion.** ``sale_ebay`` groups
  products by tier signature and manages one ``VOLUME_DISCOUNT`` promotion
  per group through a reconciler cron (auto-triggered on write). So
  :meth:`set_volume_tiers` writes ``ebay_volume_mode`` / ``ebay_volume_tier_ids``
  on the product and reports the effective label; the promotion appears on
  eBay a few minutes later without an approval step, exactly as it does when
  a person edits the product form.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ._base import BaseOps
from ..errors import OdooError

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "promotion_type", "status", "start_date", "end_date",
    "product_count", "ebay_promotion_id", "is_volume_managed", "imported",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "description", "markdown_kind", "markdown_percent", "markdown_amount",
    "tier_ids", "order_mode", "order_threshold_amount", "order_threshold_qty",
    "order_benefit_kind", "order_percent", "order_amount",
    "bogo_buy_qty", "bogo_get_qty", "bogo_percent",
    "coupon_code", "coupon_type",
    "rule_category_id", "rule_min_price", "rule_max_price",
    "product_ids", "apply_single_item_only",
    "report_items_sold", "report_sale_amount", "report_last_pulled",
    "ebay_status_note",
]

_TIER_FIELDS = ["id", "min_quantity", "percent_off"]
_PRODUCT_FIELDS = ["id", "name", "default_code", "list_price", "ebay_id",
                   "ebay_listing_status", "ebay_volume_mode",
                   "ebay_volume_effective_label", "ebay_live_promotion_count"]

PROMOTION_TYPES = ("MARKDOWN_SALE", "VOLUME_DISCOUNT", "ORDER_DISCOUNT", "CODED_COUPON")
#: Statuses in which eBay still honours the promotion.
LIVE_STATUSES = ("scheduled", "running", "paused")


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class EbayPromotionOps(BaseOps):
    """Draft, approve and manage eBay promotions through ``ebay.promotion``."""

    MODEL = "ebay.promotion"
    MODULE = "sale_ebay"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "start_date desc, id desc"
    REQUIRED_GROUPS = ("sale_ebay.group_ebay_promotions", "sales_team.group_sale_manager")
    ALLOWED_ACTIONS = frozenset({
        "action_push_to_ebay", "action_refresh_status", "action_pause",
        "action_resume", "action_end", "action_add_products_by_rule",
        "action_refresh_reports",
    })

    # ── Read ─────────────────────────────────────────────────────────

    def list_promotions(self, status: Optional[str] = None, promotion_type: Optional[str] = None,
                        include_volume_managed: bool = False, limit: int = 50) -> list[dict]:
        """Promotions, newest start first. Hides the reconciler's per-signature
        volume promotions unless asked — they are machinery, not campaigns."""
        domain: list = []
        if status:
            domain.append(["status", "in", status.split(",")])
        if promotion_type:
            domain.append(["promotion_type", "=", promotion_type])
        if not include_volume_managed:
            domain.append(["is_volume_managed", "=", False])
        return self.search(domain, limit=limit)

    def live_promotions(self, limit: int = 50) -> list[dict]:
        return self.list_promotions(status=",".join(LIVE_STATUSES), limit=limit)

    def drafts(self, limit: int = 50) -> list[dict]:
        return self.list_promotions(status="draft", limit=limit)

    def promotion(self, promo_id: int) -> dict:
        """One promotion with its tiers and first products expanded."""
        rec = self.get(promo_id)
        tier_ids = rec.get("tier_ids") or []
        rec["tiers"] = (self.client.read("ebay.promotion.tier", tier_ids, fields=_TIER_FIELDS)
                        if tier_ids else [])
        product_ids = rec.get("product_ids") or []
        rec["products"] = (self.client.read("product.template", product_ids[:40],
                                            fields=_PRODUCT_FIELDS) if product_ids else [])
        rec["products_truncated"] = max(0, len(product_ids) - 40)
        rec["summary"] = self._summary(rec)
        return rec

    def preview_products(self, category_id: Optional[int] = None, min_price: Optional[float] = None,
                         max_price: Optional[float] = None, query: Optional[str] = None,
                         limit: int = 100) -> dict:
        """Which active eBay listings a category / price / text rule would
        pull in, BEFORE anything is written. Same domain shape as
        ``action_add_products_by_rule``."""
        domain: list = [["ebay_use", "=", True], ["ebay_listing_status", "=", "Active"]]
        if category_id:
            domain.append(["categ_id", "child_of", int(category_id)])
        if min_price is not None:
            domain.append(["list_price", ">=", float(min_price)])
        if max_price is not None:
            domain.append(["list_price", "<=", float(max_price)])
        if query:
            domain.append(["name", "ilike", query])
        rows = self.client.search_read("product.template", domain, fields=_PRODUCT_FIELDS,
                                       limit=limit, order="list_price desc")
        total = self.client.search_count("product.template", domain)
        return {
            "count": total,
            "shown": len(rows),
            "products": rows,
            "already_promoted": [r["id"] for r in rows if r.get("ebay_live_promotion_count")],
            "summary": f"{total} active eBay listing(s) match; {len(rows)} shown.",
        }

    # ── Draft / edit (Odoo only) ─────────────────────────────────────

    def create_promotion(self, name: str, promotion_type: str, product_ids: Optional[list[int]] = None,
                         start_date: Optional[str] = None, end_date: Optional[str] = None,
                         days: Optional[int] = None,
                         markdown_percent: Optional[float] = None, markdown_amount: Optional[float] = None,
                         tiers: Optional[list] = None,
                         order_mode: Optional[str] = None, order_threshold_amount: Optional[float] = None,
                         order_threshold_qty: Optional[int] = None, order_percent: Optional[float] = None,
                         order_amount: Optional[float] = None,
                         bogo_buy_qty: Optional[int] = None, bogo_get_qty: Optional[int] = None,
                         bogo_percent: Optional[float] = None,
                         coupon_code: Optional[str] = None, coupon_type: Optional[str] = None,
                         category_id: Optional[int] = None, min_price: Optional[float] = None,
                         max_price: Optional[float] = None, description: Optional[str] = None) -> dict:
        """Create a DRAFT promotion in Odoo. Nothing is sent to eBay.

        ``tiers`` is ``[[qty, percent], ...]`` for VOLUME_DISCOUNT. When a
        category/price rule is given the matching listings are added on top
        of ``product_ids`` (via ``action_add_products_by_rule``).
        """
        self._require()
        if promotion_type not in PROMOTION_TYPES:
            raise OdooError(f"promotion_type must be one of {', '.join(PROMOTION_TYPES)}")
        now = datetime.now(timezone.utc)
        start = start_date or _stamp(now + timedelta(minutes=10))
        end = end_date
        if not end and days:
            end = _stamp(now + timedelta(days=int(days)))
        vals: dict[str, Any] = {
            "name": name, "promotion_type": promotion_type, "start_date": start,
        }
        if end:
            vals["end_date"] = end
        if description:
            vals["description"] = description
        if product_ids:
            vals["product_ids"] = [[6, 0, [int(p) for p in product_ids]]]
        vals.update(self._type_vals(
            promotion_type, markdown_percent=markdown_percent, markdown_amount=markdown_amount,
            tiers=tiers, order_mode=order_mode, order_threshold_amount=order_threshold_amount,
            order_threshold_qty=order_threshold_qty, order_percent=order_percent,
            order_amount=order_amount, bogo_buy_qty=bogo_buy_qty, bogo_get_qty=bogo_get_qty,
            bogo_percent=bogo_percent, coupon_code=coupon_code, coupon_type=coupon_type))
        if category_id:
            vals["rule_category_id"] = int(category_id)
        if min_price is not None:
            vals["rule_min_price"] = float(min_price)
        if max_price is not None:
            vals["rule_max_price"] = float(max_price)
        promo_id = self.client.create(self.MODEL, vals)
        if category_id or min_price is not None or max_price is not None:
            self.client.execute(self.MODEL, "action_add_products_by_rule", [promo_id])
        out = self.promotion(promo_id)
        out["summary"] = "DRAFT (not on eBay yet). " + out["summary"] + \
            f" Say `approve promo {promo_id}` to push it."
        return out

    def update_promotion(self, promo_id: int, **changes: Any) -> dict:
        """Edit a promotion's fields in Odoo. A live promotion stays live on
        eBay with its OLD values until :meth:`approve_promotion` re-pushes."""
        self._require()
        rec = self.get(promo_id, fields=["status", "promotion_type", "is_volume_managed"])
        if rec.get("is_volume_managed"):
            raise OdooError("This promotion is reconciler-managed; edit the products' "
                            "volume tiers instead (set_volume_tiers).")
        vals: dict[str, Any] = {}
        tiers = changes.pop("tiers", None)
        add_products = changes.pop("add_product_ids", None)
        remove_products = changes.pop("remove_product_ids", None)
        product_ids = changes.pop("product_ids", None)
        for k, v in changes.items():
            if k in _DETAIL_FIELDS and k not in ("tier_ids", "product_ids", "product_count",
                                                  "ebay_promotion_id", "status"):
                vals[k] = v
        if tiers is not None:
            vals["tier_ids"] = [[5, 0, 0]] + [
                [0, 0, {"min_quantity": int(q), "percent_off": float(p)}] for q, p in tiers]
        if product_ids is not None:
            vals["product_ids"] = [[6, 0, [int(p) for p in product_ids]]]
        else:
            ops = [[4, int(p)] for p in (add_products or [])]
            ops += [[3, int(p)] for p in (remove_products or [])]
            if ops:
                vals["product_ids"] = ops
        if not vals:
            raise OdooError("Nothing to update.")
        self.client.write(self.MODEL, promo_id, vals)
        out = self.promotion(promo_id)
        if rec.get("status") in LIVE_STATUSES:
            out["summary"] += f" Live on eBay with OLD values until `approve promo {promo_id}`."
        return out

    @staticmethod
    def _type_vals(promotion_type: str, **kw: Any) -> dict:
        """Benefit fields for one promotion type; rejects a type with no benefit."""
        vals: dict[str, Any] = {}
        if promotion_type == "MARKDOWN_SALE":
            if kw.get("markdown_amount") is not None:
                vals.update(markdown_kind="amount", markdown_amount=float(kw["markdown_amount"]))
            elif kw.get("markdown_percent") is not None:
                vals.update(markdown_kind="percent", markdown_percent=float(kw["markdown_percent"]))
            else:
                raise OdooError("MARKDOWN_SALE needs markdown_percent or markdown_amount.")
        elif promotion_type == "VOLUME_DISCOUNT":
            tiers = kw.get("tiers") or []
            clean = sorted((int(q), float(p)) for q, p in tiers)
            if not clean or any(q < 2 or not (0 < p < 100) for q, p in clean):
                raise OdooError("VOLUME_DISCOUNT needs tiers=[[qty, pct], ...] with qty >= 2, 0 < pct < 100.")
            vals["tier_ids"] = [[0, 0, {"min_quantity": q, "percent_off": p}] for q, p in clean]
        elif promotion_type == "ORDER_DISCOUNT":
            mode = kw.get("order_mode") or ("bogo" if kw.get("bogo_get_qty") else
                                            "quantity" if kw.get("order_threshold_qty") else "spend")
            vals["order_mode"] = mode
            if mode == "bogo":
                vals.update(bogo_buy_qty=int(kw.get("bogo_buy_qty") or 1),
                            bogo_get_qty=int(kw.get("bogo_get_qty") or 1),
                            bogo_percent=float(kw.get("bogo_percent") or 100.0))
            else:
                if mode == "spend":
                    if kw.get("order_threshold_amount") is None:
                        raise OdooError("ORDER_DISCOUNT (spend) needs order_threshold_amount.")
                    vals["order_threshold_amount"] = float(kw["order_threshold_amount"])
                else:
                    vals["order_threshold_qty"] = int(kw.get("order_threshold_qty") or 2)
                if kw.get("order_amount") is not None:
                    vals.update(order_benefit_kind="amount", order_amount=float(kw["order_amount"]))
                elif kw.get("order_percent") is not None:
                    vals.update(order_benefit_kind="percent", order_percent=float(kw["order_percent"]))
                else:
                    raise OdooError("ORDER_DISCOUNT needs order_percent or order_amount.")
        elif promotion_type == "CODED_COUPON":
            # Coupons share the ORDER benefit fields (order_percent/order_amount)
            # and an optional spend/qty threshold — not the markdown fields.
            code = (kw.get("coupon_code") or "").strip()
            if not code:
                raise OdooError("CODED_COUPON needs coupon_code (8-15 alphanumerics).")
            vals["coupon_code"] = code
            vals["coupon_type"] = kw.get("coupon_type") or "PUBLIC_SINGLE_SELLER_COUPON"
            pct = kw.get("order_percent", kw.get("markdown_percent"))
            amt = kw.get("order_amount", kw.get("markdown_amount"))
            if amt is not None:
                vals.update(order_benefit_kind="amount", order_amount=float(amt))
            elif pct is not None:
                vals.update(order_benefit_kind="percent", order_percent=float(pct))
            else:
                raise OdooError("CODED_COUPON needs order_percent or order_amount.")
            if kw.get("order_threshold_qty"):
                vals.update(order_mode="quantity", order_threshold_qty=int(kw["order_threshold_qty"]))
            else:
                vals.update(order_mode="spend",
                            order_threshold_amount=float(kw.get("order_threshold_amount") or 0.0))
        return vals

    # ── eBay side (gated) ────────────────────────────────────────────

    def approve_promotion(self, promo_id: int) -> dict:
        """Push the promotion to eBay (create or update). The operator's
        literal ``approve promo <id>`` is the only trigger for this."""
        self._require()
        rec = self.get(promo_id, fields=["status", "product_count", "name", "promotion_type",
                                         "start_date", "end_date", "tier_ids"])
        if not rec.get("product_count"):
            raise OdooError(f"Promotion {promo_id} has no products; add some before approving.")
        if rec.get("status") == "ended":
            raise OdooError(f"Promotion {promo_id} has ended; create a new one instead.")
        result = self.run_action(promo_id, "action_push_to_ebay")
        out = self.promotion(promo_id)
        out["pushed"] = True
        out["summary"] = "PUSHED to eBay. " + out["summary"]
        out["action_result"] = result.get("returned")
        return out

    def pause_promotion(self, promo_id: int) -> dict:
        return self._lifecycle(promo_id, "action_pause")

    def resume_promotion(self, promo_id: int) -> dict:
        return self._lifecycle(promo_id, "action_resume")

    def end_promotion(self, promo_id: int) -> dict:
        """End (delete) on eBay and mark ended in Odoo. Irreversible."""
        return self._lifecycle(promo_id, "action_end")

    def refresh_promotion(self, promo_id: int) -> dict:
        """Re-read eBay status and performance numbers."""
        self._require()
        rec = self.get(promo_id, fields=["ebay_promotion_id"])
        if not rec.get("ebay_promotion_id"):
            raise OdooError(f"Promotion {promo_id} was never pushed; nothing to refresh.")
        self.client.execute(self.MODEL, "action_refresh_status", [promo_id])
        self.client.execute(self.MODEL, "action_refresh_reports", [promo_id])
        return self.promotion(promo_id)

    def _lifecycle(self, promo_id: int, method: str) -> dict:
        self._require()
        rec = self.get(promo_id, fields=["ebay_promotion_id", "status"])
        if method != "action_end" and not rec.get("ebay_promotion_id"):
            raise OdooError(f"Promotion {promo_id} is not on eBay (status "
                            f"{rec.get('status')}); {method} needs a pushed promotion.")
        self.run_action(promo_id, method)
        return self.promotion(promo_id)

    # ── Per-product volume tiers ─────────────────────────────────────

    def volume_tiers(self, product_tmpl_id: int) -> dict:
        """Effective volume discount for a product (mode, tiers, label)."""
        self._require()
        rows = self.client.read("product.template", [int(product_tmpl_id)], fields=[
            "id", "name", "default_code", "ebay_volume_mode", "ebay_volume_tier_ids",
            "ebay_volume_effective_label", "ebay_volume_promotion_id",
            "ebay_live_promotion_count", "ebay_listing_status", "categ_id"])
        if not rows:
            raise OdooError(f"No product.template {product_tmpl_id}")
        rec = rows[0]
        tier_ids = rec.get("ebay_volume_tier_ids") or []
        rec["tiers"] = (self.client.read("ebay.volume.tier", tier_ids, fields=_TIER_FIELDS)
                        if tier_ids else [])
        rec["summary"] = (f"{rec.get('default_code') or rec['id']}: {rec.get('ebay_volume_mode')} — "
                          f"{rec.get('ebay_volume_effective_label') or 'no volume discount'}")
        return rec

    def set_volume_tiers(self, product_tmpl_id: int, tiers: Optional[list] = None,
                         mode: Optional[str] = None) -> dict:
        """Set a product's volume discount.

        ``tiers=[[2, 5], [3, 10]]`` → custom tiers; ``mode='inherit'`` → use the
        category default; ``mode='none'`` → opt out. The reconciler cron
        creates / moves the eBay VOLUME_DISCOUNT promotion within minutes.
        """
        self._require()
        if tiers:
            clean = sorted((int(q), float(p)) for q, p in tiers)
            if any(q < 2 or not (0 < p < 100) for q, p in clean):
                raise OdooError("Each tier needs qty >= 2 and 0 < percent < 100.")
            vals = {"ebay_volume_mode": "custom",
                    "ebay_volume_tier_ids": [[5, 0, 0]] + [
                        [0, 0, {"min_quantity": q, "percent_off": p}] for q, p in clean]}
        elif mode in ("inherit", "none"):
            vals = {"ebay_volume_mode": mode}
        else:
            raise OdooError("Give tiers=[[qty, pct], ...] or mode='inherit' | 'none'.")
        self.client.write("product.template", int(product_tmpl_id), vals)
        out = self.volume_tiers(product_tmpl_id)
        out["written"] = True
        return out

    # ── Summaries ────────────────────────────────────────────────────

    @staticmethod
    def _summary(rec: dict) -> str:
        kind = rec.get("promotion_type")
        if kind == "MARKDOWN_SALE":
            benefit = (f"{rec.get('markdown_percent') or 0:g}% off" if rec.get("markdown_kind") != "amount"
                       else f"{rec.get('markdown_amount') or 0:.2f} off")
        elif kind == "VOLUME_DISCOUNT":
            benefit = ", ".join(f"{t['min_quantity']}+ → {t['percent_off']:g}%"
                                for t in rec.get("tiers") or []) or "no tiers"
        elif kind == "ORDER_DISCOUNT":
            mode = rec.get("order_mode")
            if mode == "bogo":
                benefit = (f"buy {rec.get('bogo_buy_qty')} get {rec.get('bogo_get_qty')} "
                           f"at {rec.get('bogo_percent') or 0:g}% off")
            else:
                cond = (f"spend {rec.get('order_threshold_amount') or 0:.2f}" if mode == "spend"
                        else f"buy {rec.get('order_threshold_qty')}+")
                ben = (f"{rec.get('order_percent') or 0:g}% off" if rec.get("order_benefit_kind") != "amount"
                       else f"{rec.get('order_amount') or 0:.2f} off")
                benefit = f"{cond} → {ben}"
        elif kind == "CODED_COUPON":
            benefit = f"code {rec.get('coupon_code') or '?'}"
        else:
            benefit = kind or "?"
        window = f"{rec.get('start_date') or '?'} → {rec.get('end_date') or 'open-ended'}"
        return (f"#{rec.get('id')} {rec.get('name')} [{kind}] {rec.get('status')}: {benefit}; "
                f"{rec.get('product_count') or 0} product(s); {window}"
                + (f"; eBay {rec['ebay_promotion_id']}" if rec.get("ebay_promotion_id") else ""))

    def promotions_summary(self) -> dict:
        """One-screen overview for the agent."""
        live = self.live_promotions(limit=20)
        drafts = self.drafts(limit=20)
        return {
            "live": live, "drafts": drafts,
            "summary": (f"{len(live)} live promotion(s), {len(drafts)} draft(s)."
                        + (" Drafts: " + "; ".join(f"#{d['id']} {d['name']}" for d in drafts)
                           if drafts else "")),
        }
