"""
eBay buyer Best Offers for ``sale_ebay`` — ``ebay.best.offer``.

Odoo mirrors every open offer (Trading GetBestOffers, 15-minute cron), so
the agent never talks to eBay for the list. What it adds:

* **Research on request.** :meth:`offer` returns the offer with the
  product's stored comps; the skill's browser step gathers *sold* prices
  and :meth:`record_review` stores the verdict on the offer
  (``fair`` when offer >= 0.85 x sold median, ``high`` >= 1.05x).

* **Responding is a separate, gated write.** :meth:`accept_offer`,
  :meth:`decline_offer` and :meth:`counter_offer` each hit
  RespondToBestOffer once. The operator's literal ``accept offer <id>`` /
  ``counter offer <id> <price>`` / ``decline offer <id>`` is the only
  trigger; nothing here decides on its own.

* **The buyer gets told why.** eBay shows ``SellerResponse`` only inside the
  offer card, which buyers miss. Passing ``buyer_message`` to a response
  also sends an eBay member message (AddMemberMessageAAQToPartner) so the
  reasoning lands in their inbox. :meth:`message_buyer` does it standalone.
"""

import logging
from typing import Any, Optional

from datetime import timedelta

from ._base import BaseOps, utc_stamp
from ..errors import OdooError

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "best_offer_id", "item_id", "item_title", "product_tmpl_id",
    "buyer_user_id", "buyer_feedback_score", "offer_type", "offer_price",
    "quantity", "list_price_at_offer", "discount_pct", "status",
    "expiration_time", "received_at", "review_verdict", "review_median",
    "review_n",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "buyer_state", "buyer_country", "message", "ebay_status_raw",
    "last_seen_at", "is_expired", "review_note", "review_at", "review_ratio",
    "response_action", "counter_price", "response_message", "responded_at",
    "responded_by", "buyer_notice", "buyer_notice_sent_at",
]

_PRODUCT_FIELDS = [
    "id", "name", "default_code", "list_price", "standard_price", "ebay_id",
    "ebay_listing_status", "ebay_comp_low", "ebay_comp_p25", "ebay_comp_median",
    "ebay_comp_high", "ebay_comp_count", "ebay_comp_fetched_at",
    "ebay_suggested_price", "qty_available",
]

REVIEW_FAIR_RATIO = 0.85
REVIEW_HIGH_RATIO = 1.05


def _ref_id(value: Any) -> Any:
    return value[0] if isinstance(value, (list, tuple)) and value else value or None


def verdict_for(offer_price: float, sold_median: float) -> str:
    """Same thresholds as ``ebay.best.offer.verdict_for`` on the server."""
    if not sold_median or sold_median <= 0 or not offer_price:
        return "unknown"
    ratio = float(offer_price) / float(sold_median)
    if ratio >= REVIEW_HIGH_RATIO:
        return "high"
    if ratio >= REVIEW_FAIR_RATIO:
        return "fair"
    return "low"


class EbayBestOfferOps(BaseOps):
    """Read, review and answer buyer Best Offers mirrored into Odoo."""

    MODEL = "ebay.best.offer"
    MODULE = "sale_ebay"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "received_at desc, id desc"
    REQUIRED_GROUPS = ("sale_ebay.group_ebay_offers", "sales_team.group_sale_manager")
    ALLOWED_ACTIONS = frozenset({
        "action_accept", "action_decline", "action_counter",
        "action_record_review", "action_message_buyer", "action_sync_now",
    })

    # ── Read ─────────────────────────────────────────────────────────

    def open_offers(self, limit: int = 20) -> list[dict]:
        """Open offers newest first, with product comps and cost — the
        server-side summary rows (``open_offers_summary``)."""
        self._require()
        return self.client.execute(self.MODEL, "open_offers_summary", limit=limit) or []

    def offers_for_product(self, product_tmpl_id: int, include_closed: bool = False,
                           limit: int = 20) -> list[dict]:
        domain: list = [["product_tmpl_id", "=", int(product_tmpl_id)]]
        if not include_closed:
            domain.append(["status", "=", "pending"])
        return self.search(domain, limit=limit)

    def recent_offers(self, days: int = 7, limit: int = 50) -> list[dict]:
        since = utc_stamp(-timedelta(days=int(days)))
        return self.search([["received_at", ">=", since]], limit=limit)

    def offer(self, offer_id: int) -> dict:
        """One offer with the linked product's pricing context attached."""
        rec = self._resolve(offer_id)
        tmpl_id = _ref_id(rec.get("product_tmpl_id"))
        rec["product"] = (self.client.read("product.template", [tmpl_id], fields=_PRODUCT_FIELDS) or [{}])[0] \
            if tmpl_id else {}
        rec["suggested_verdict"] = verdict_for(rec.get("offer_price") or 0.0,
                                               (rec["product"] or {}).get("ebay_comp_median") or 0.0)
        rec["summary"] = self._summary(rec)
        return rec

    def _resolve(self, offer_id: Any) -> dict:
        """Accept an Odoo id or an eBay BestOfferID."""
        self._require()
        try:
            return self.get(int(offer_id))
        except (OdooError, ValueError, TypeError):
            rows = self.search([["best_offer_id", "=", str(offer_id)]], limit=1,
                               fields=self._fields(detail=True))
            if not rows:
                raise OdooError(f"No eBay offer with id or BestOfferID {offer_id!r}")
            return rows[0]

    # ── Review ───────────────────────────────────────────────────────

    def record_review(self, offer_id: int, sold_median: float, sold_n: int,
                      note: Optional[str] = None, verdict: Optional[str] = None,
                      prices: Optional[list[float]] = None) -> dict:
        """Store a sold-comps review on the offer. When ``prices`` (the raw
        sold prices) are given they are also written to the product's comp
        aggregates via ``ebay.set_sold_comps`` semantics, so the product
        keeps the research."""
        rec = self._resolve(offer_id)
        rid = rec["id"]
        # XML-RPC cannot marshal None: only pass the optional kwargs that are set.
        kwargs = {k: v for k, v in (("note", note), ("verdict", verdict)) if v}
        out = self.client.execute(self.MODEL, "action_record_review", [rid],
                                  float(sold_median or 0.0), int(sold_n or 0), **kwargs)
        result = self.offer(rid)
        result["review"] = out
        result["summary"] = self._summary(result)
        return result

    # ── Respond (gated) ──────────────────────────────────────────────

    def accept_offer(self, offer_id: int, message: Optional[str] = None,
                     buyer_message: Optional[str] = None) -> dict:
        return self._respond(offer_id, "action_accept", message=message, buyer_message=buyer_message)

    def decline_offer(self, offer_id: int, message: Optional[str] = None,
                      buyer_message: Optional[str] = None) -> dict:
        return self._respond(offer_id, "action_decline", message=message, buyer_message=buyer_message)

    def counter_offer(self, offer_id: int, price: float, message: Optional[str] = None,
                      buyer_message: Optional[str] = None, quantity: Optional[int] = None) -> dict:
        """Counter at ``price`` (must be above the offer and below list)."""
        rec = self._resolve(offer_id)
        price = float(price)
        if price <= float(rec.get("offer_price") or 0):
            raise OdooError(f"Counter {price:.2f} must be above the buyer's "
                            f"{rec.get('offer_price'):.2f}.")
        lp = float(rec.get("list_price_at_offer") or 0)
        if lp and price >= lp:
            raise OdooError(f"Counter {price:.2f} must be below the list price {lp:.2f}.")
        return self._respond(rec["id"], "action_counter", price, quantity=quantity,
                             message=message, buyer_message=buyer_message)

    def _respond(self, offer_id: Any, method: str, *args: Any, message: Optional[str] = None,
                 buyer_message: Optional[str] = None, **kwargs: Any) -> dict:
        rec = self._resolve(offer_id)
        if rec.get("status") != "pending":
            raise OdooError(f"Offer {rec['id']} is {rec.get('status')}; only open offers can be answered.")
        if rec.get("is_expired"):
            raise OdooError(f"Offer {rec['id']} expired at {rec.get('expiration_time')}.")
        rid = rec["id"]
        call_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if message:
            call_kwargs["message"] = message
        self.client.execute(self.MODEL, method, [rid], *args, **call_kwargs)
        notice = None
        if buyer_message:
            try:
                self.client.execute(self.MODEL, "action_message_buyer", [rid], buyer_message)
                notice = "sent"
            except OdooError as exc:  # the response already went through; report, don't unwind
                logger.warning("buyer message after %s on offer %s failed: %s", method, rid, exc)
                notice = f"FAILED: {str(exc)[:200]}"
        out = self.offer(rid)
        out["buyer_message_status"] = notice
        out["summary"] = f"{method.replace('action_', '').upper()}ED. " + out["summary"] + \
            (f" Buyer message: {notice}." if notice else "")
        return out

    def message_buyer(self, offer_id: int, body: str, subject: Optional[str] = None) -> dict:
        """Send the buyer an eBay member message about the listing."""
        rec = self._resolve(offer_id)
        kwargs = {"subject": subject} if subject else {}
        self.client.execute(self.MODEL, "action_message_buyer", [rec["id"]], body, **kwargs)
        out = self.offer(rec["id"])
        out["summary"] = "Buyer messaged. " + out["summary"]
        return out

    def sync_offers(self) -> dict:
        """Pull open offers from eBay now instead of waiting for the cron."""
        self._require()
        raw = self.client.execute(self.MODEL, "action_sync_now")
        params = (raw or {}).get("params") if isinstance(raw, dict) else {}
        return {"synced": True, "summary": (params or {}).get("message") or "Synced.",
                "open": self.open_offers(limit=20)}

    # ── Summary ──────────────────────────────────────────────────────

    @staticmethod
    def _summary(rec: dict) -> str:
        product = rec.get("product") or {}
        parts = [
            f"Offer #{rec.get('id')} ({rec.get('best_offer_id')}) {rec.get('status')}: "
            f"{rec.get('offer_price') or 0:.2f} for {rec.get('quantity') or 1}x "
            f"\"{(rec.get('item_title') or '')[:60]}\" list {rec.get('list_price_at_offer') or 0:.2f} "
            f"({rec.get('discount_pct') or 0:.1f}% below)",
            f"buyer {rec.get('buyer_user_id')} ({rec.get('buyer_feedback_score') or 0} fb)",
        ]
        if rec.get("review_verdict"):
            parts.append(f"review {rec['review_verdict']} vs sold median "
                         f"{rec.get('review_median') or 0:.2f} (n={rec.get('review_n') or 0})")
        elif product.get("ebay_comp_median"):
            parts.append(f"stored comps median {product['ebay_comp_median']:.2f} "
                         f"(n={product.get('ebay_comp_count') or 0}, asks) → {rec.get('suggested_verdict')}")
        else:
            parts.append("no comps yet")
        if product.get("standard_price"):
            parts.append(f"cost {product['standard_price']:.2f}")
        if rec.get("message"):
            parts.append(f"buyer says: {rec['message'][:120]!r}")
        if rec.get("expiration_time"):
            parts.append(f"expires {rec['expiration_time']}")
        return "; ".join(parts)

    def offers_summary(self) -> dict:
        rows = self.open_offers(limit=20)
        unreviewed = [r for r in rows if not r.get("review_verdict")]
        return {
            "open": rows,
            "summary": (f"{len(rows)} open offer(s), {len(unreviewed)} unreviewed."
                        + ("".join(f"\n- #{r['id']} {r.get('offer_price'):.2f} vs list "
                                   f"{r.get('list_price') or 0:.2f} on {(r.get('title') or '')[:50]} "
                                   f"({r.get('review_verdict') or 'unreviewed'})" for r in rows[:10]))),
        }
