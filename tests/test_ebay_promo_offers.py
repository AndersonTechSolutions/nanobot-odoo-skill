"""EbayPromotionOps / EbayBestOfferOps — the connector half of Kevin's eBay
promotions + Best Offer review feature.

A routing side-effect answers ``execute_kw`` by (model, method) so the
assertions are about what reached Odoo, not about call ordering.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.errors import OdooError  # noqa: E402
from odoo_skill.models.ebay_best_offer import EbayBestOfferOps, verdict_for  # noqa: E402
from odoo_skill.models.ebay_promotion import EbayPromotionOps  # noqa: E402


def _calls(mock_client):
    out = []
    for call in mock_client._models.execute_kw.call_args_list:
        a = call[0]
        out.append((a[3], a[4], a[5], a[6] if len(a) > 6 else {}))
    return out


def _ready(cls, mock_client):
    ops = cls(mock_client)
    ops._available = True
    ops._model_field_cache = set()
    return ops


class Router:
    """Answer execute_kw from a table keyed by (model, method); record misses."""

    def __init__(self, table):
        self.table = table
        self.misses = []

    def __call__(self, db, uid, key, model, method, args, kwargs=None):
        handler = self.table.get((model, method))
        if handler is None:
            self.misses.append((model, method))
            return True
        return handler(args, kwargs or {}) if callable(handler) else handler


PROMO = {"id": 5, "name": "Spring Sale", "promotion_type": "MARKDOWN_SALE", "status": "draft",
         "start_date": "2026-09-08 00:00:00", "end_date": False, "product_count": 2,
         "ebay_promotion_id": False, "is_volume_managed": False, "imported": False,
         "markdown_kind": "percent", "markdown_percent": 15.0, "tier_ids": [], "product_ids": [7, 8]}


@pytest.fixture()
def promo(mock_client):
    return _ready(EbayPromotionOps, mock_client)


@pytest.fixture()
def offers(mock_client):
    return _ready(EbayBestOfferOps, mock_client)


class TestPromotions:

    def test_list_hides_reconciler_promotions_by_default(self, promo, mock_client):
        mock_client._models.execute_kw.side_effect = Router({("ebay.promotion", "search_read"): [PROMO]})
        promo.list_promotions(status="running,scheduled")
        _, _, args, kw = _calls(mock_client)[0]
        assert ["is_volume_managed", "=", False] in args[0]
        assert ["status", "in", ["running", "scheduled"]] in args[0]

    def test_create_promotion_is_a_draft_with_no_ebay_call(self, promo, mock_client):
        state = {}

        def create(args, kw):
            state["vals"] = args[0]
            return 5

        r = Router({("ebay.promotion", "create"): create,
                    ("ebay.promotion", "read"): [PROMO],
                    ("product.template", "read"): [{"id": 7, "name": "A"}, {"id": 8, "name": "B"}]})
        mock_client._models.execute_kw.side_effect = r
        out = promo.create_promotion("Spring Sale", "MARKDOWN_SALE", product_ids=[7, 8],
                                     markdown_percent=15, days=7)
        vals = state["vals"]
        assert vals["promotion_type"] == "MARKDOWN_SALE"
        assert vals["markdown_kind"] == "percent" and vals["markdown_percent"] == 15.0
        assert vals["product_ids"] == [[6, 0, [7, 8]]]
        assert vals["end_date"] and vals["start_date"]
        assert "action_push_to_ebay" not in [m for _, m in r.misses] + [c[1] for c in _calls(mock_client)]
        assert out["summary"].startswith("DRAFT") and "approve promo 5" in out["summary"]

    def test_create_coupon_accepts_markdown_alias_through_public_method(self, promo, mock_client):
        state = {}

        def create(args, kw):
            state["vals"] = args[0]
            return 6

        r = Router({("ebay.promotion", "create"): create,
                    ("ebay.promotion", "read"): [dict(PROMO, id=6, promotion_type="CODED_COUPON")],
                    ("product.template", "read"): [{"id": 7, "name": "A"}]})
        mock_client._models.execute_kw.side_effect = r
        promo.create_promotion("Coupon", "CODED_COUPON", product_ids=[7],
                               coupon_code="SAVE10NOW", markdown_percent=10, days=7)
        vals = state["vals"]
        assert vals["order_benefit_kind"] == "percent" and vals["order_percent"] == 10.0
        assert "markdown_percent" not in vals

    def test_create_volume_promotion_builds_tiers(self, promo, mock_client):
        state = {}
        r = Router({("ebay.promotion", "create"): lambda a, k: state.setdefault("vals", a[0]) and 6,
                    ("ebay.promotion", "read"): [dict(PROMO, id=6, promotion_type="VOLUME_DISCOUNT", tier_ids=[1, 2])],
                    ("ebay.promotion.tier", "read"): [{"id": 1, "min_quantity": 2, "percent_off": 5.0},
                                                      {"id": 2, "min_quantity": 3, "percent_off": 10.0}],
                    ("product.template", "read"): []})
        mock_client._models.execute_kw.side_effect = r
        out = promo.create_promotion("BMSM", "VOLUME_DISCOUNT", product_ids=[7], tiers=[[2, 5], [3, 10]])
        assert state["vals"]["tier_ids"] == [[0, 0, {"min_quantity": 2, "percent_off": 5.0}],
                                             [0, 0, {"min_quantity": 3, "percent_off": 10.0}]]
        assert "2+ → 5%" in out["summary"] and "3+ → 10%" in out["summary"]

    def test_create_with_category_rule_runs_the_rule_action(self, promo, mock_client):
        r = Router({("ebay.promotion", "create"): 5, ("ebay.promotion", "read"): [PROMO],
                    ("product.template", "read"): []})
        mock_client._models.execute_kw.side_effect = r
        promo.create_promotion("Cat sale", "MARKDOWN_SALE", markdown_percent=10, category_id=42, min_price=50)
        calls = _calls(mock_client)
        assert ("ebay.promotion", "action_add_products_by_rule", [[5]]) in [(c[0], c[1], c[2]) for c in calls]
        create_vals = [c for c in calls if c[1] == "create"][0][2][0]
        assert create_vals["rule_category_id"] == 42 and create_vals["rule_min_price"] == 50.0

    def test_create_rejects_bad_type_and_tier_shape(self, promo, mock_client):
        with pytest.raises(OdooError):
            promo.create_promotion("x", "FREE_STUFF")
        with pytest.raises(OdooError):
            promo.create_promotion("x", "VOLUME_DISCOUNT", tiers=[[1, 5]])
        with pytest.raises(OdooError):
            promo.create_promotion("x", "MARKDOWN_SALE")   # no benefit
        assert not _calls(mock_client)

    def test_approve_pushes_and_refuses_empty(self, promo, mock_client):
        r = Router({("ebay.promotion", "read"): [dict(PROMO, product_count=0)]})
        mock_client._models.execute_kw.side_effect = r
        with pytest.raises(OdooError, match="no products"):
            promo.approve_promotion(5)
        assert "action_push_to_ebay" not in [c[1] for c in _calls(mock_client)]

        mock_client._models.execute_kw.reset_mock()
        r = Router({("ebay.promotion", "read"): [dict(PROMO, status="scheduled", ebay_promotion_id="P-1")],
                    ("ebay.promotion", "action_push_to_ebay"): True,
                    ("product.template", "read"): []})
        mock_client._models.execute_kw.side_effect = r
        out = promo.approve_promotion(5)
        assert ("ebay.promotion", "action_push_to_ebay", [[5]]) in [(c[0], c[1], c[2]) for c in _calls(mock_client)]
        assert out["pushed"] is True and out["summary"].startswith("PUSHED")

    def test_pause_needs_a_pushed_promotion(self, promo, mock_client):
        mock_client._models.execute_kw.side_effect = Router({("ebay.promotion", "read"): [PROMO]})
        with pytest.raises(OdooError, match="not on eBay"):
            promo.pause_promotion(5)

    def test_update_refuses_reconciler_managed(self, promo, mock_client):
        mock_client._models.execute_kw.side_effect = Router(
            {("ebay.promotion", "read"): [dict(PROMO, is_volume_managed=True)]})
        with pytest.raises(OdooError, match="reconciler"):
            promo.update_promotion(5, markdown_percent=20)

    def test_update_live_warns_about_repush(self, promo, mock_client):
        state = {}
        r = Router({("ebay.promotion", "read"): [dict(PROMO, status="running", ebay_promotion_id="P-1")],
                    ("ebay.promotion", "write"): lambda a, k: state.setdefault("vals", a[1]) and True,
                    ("product.template", "read"): []})
        mock_client._models.execute_kw.side_effect = r
        out = promo.update_promotion(5, markdown_percent=20, add_product_ids=[9], remove_product_ids=[7])
        assert state["vals"]["markdown_percent"] == 20
        assert state["vals"]["product_ids"] == [[4, 9], [3, 7]]
        assert "OLD values" in out["summary"]

    def test_preview_products_domain(self, promo, mock_client):
        r = Router({("product.template", "search_read"): [{"id": 7, "ebay_live_promotion_count": 1}],
                    ("product.template", "search_count"): 1})
        mock_client._models.execute_kw.side_effect = r
        out = promo.preview_products(category_id=3, min_price=20, max_price=200, query="dell")
        dom = _calls(mock_client)[0][2][0]
        assert ["ebay_listing_status", "=", "Active"] in dom
        assert ["categ_id", "child_of", 3] in dom
        assert ["list_price", ">=", 20.0] in dom and ["list_price", "<=", 200.0] in dom
        assert ["name", "ilike", "dell"] in dom
        assert out["already_promoted"] == [7]

    def test_set_volume_tiers_writes_custom_mode(self, promo, mock_client):
        state = {}
        r = Router({("product.template", "write"): lambda a, k: state.setdefault("vals", a[1]) and True,
                    ("product.template", "read"): [{"id": 7, "default_code": "X", "ebay_volume_mode": "custom",
                                                    "ebay_volume_tier_ids": [], "ebay_volume_effective_label": "2+ 5%"}]})
        mock_client._models.execute_kw.side_effect = r
        out = promo.set_volume_tiers(7, tiers=[[3, 10], [2, 5]])
        assert state["vals"]["ebay_volume_mode"] == "custom"
        assert state["vals"]["ebay_volume_tier_ids"][0] == [5, 0, 0]
        assert state["vals"]["ebay_volume_tier_ids"][1] == [0, 0, {"min_quantity": 2, "percent_off": 5.0}]
        assert out["written"] is True

    def test_set_volume_tiers_validates(self, promo, mock_client):
        with pytest.raises(OdooError):
            promo.set_volume_tiers(7, tiers=[[1, 5]])
        with pytest.raises(OdooError):
            promo.set_volume_tiers(7, mode="sometimes")
        assert not _calls(mock_client)


OFFER = {"id": 3, "best_offer_id": "7001", "item_id": "110", "item_title": "Dell Latitude 5540",
         "product_tmpl_id": [7, "Dell Latitude 5540"], "buyer_user_id": "buyer_one",
         "buyer_feedback_score": 42, "offer_type": "BuyerBestOffer", "offer_price": 120.0,
         "quantity": 1, "list_price_at_offer": 199.99, "discount_pct": 40.0, "status": "pending",
         "expiration_time": "2026-09-09 10:00:00", "received_at": "2026-09-07 10:00:00",
         "review_verdict": False, "review_median": 0.0, "review_n": 0, "is_expired": False,
         "message": "would you take 120?"}
PRODUCT = {"id": 7, "name": "Dell Latitude 5540", "default_code": "LAT", "list_price": 199.99,
           "standard_price": 80.0, "ebay_comp_median": 150.0, "ebay_comp_count": 9}


class TestBestOffers:

    def test_verdict_thresholds(self):
        assert verdict_for(85, 100) == "fair"
        assert verdict_for(84.9, 100) == "low"
        assert verdict_for(105, 100) == "high"
        assert verdict_for(50, 0) == "unknown"

    def test_open_offers_uses_server_summary(self, offers, mock_client):
        rows = [{"id": 3, "offer_price": 120.0, "list_price": 199.99, "title": "Dell", "review_verdict": False}]
        mock_client._models.execute_kw.side_effect = Router({("ebay.best.offer", "open_offers_summary"): rows})
        out = offers.offers_summary()
        assert out["open"] == rows and "1 unreviewed" in out["summary"]
        assert _calls(mock_client)[0][3] == {"limit": 20}

    def test_offer_attaches_product_and_suggested_verdict(self, offers, mock_client):
        mock_client._models.execute_kw.side_effect = Router(
            {("ebay.best.offer", "read"): [OFFER], ("product.template", "read"): [PRODUCT]})
        out = offers.offer(3)
        assert out["product"]["ebay_comp_median"] == 150.0
        assert out["suggested_verdict"] == "low"      # 120/150 = 0.8
        assert "cost 80.00" in out["summary"] and "buyer says" in out["summary"]

    def test_offer_resolves_ebay_best_offer_id(self, offers, mock_client):
        def read(args, kw):
            if args[0] == [7001]:
                return []
            return [OFFER]
        r = Router({("ebay.best.offer", "read"): read,
                    ("ebay.best.offer", "search_read"): [OFFER],
                    ("product.template", "read"): [PRODUCT]})
        mock_client._models.execute_kw.side_effect = r
        out = offers.offer("7001")
        assert out["id"] == 3
        assert any(c[1] == "search_read" and ["best_offer_id", "=", "7001"] in c[2][0]
                   for c in _calls(mock_client))

    def test_record_review_calls_server_and_returns_verdict(self, offers, mock_client):
        r = Router({("ebay.best.offer", "read"): [dict(OFFER, review_verdict="fair", review_median=135.0, review_n=6)],
                    ("ebay.best.offer", "action_record_review"): {"review_verdict": "fair"},
                    ("product.template", "read"): [PRODUCT]})
        mock_client._models.execute_kw.side_effect = r
        out = offers.record_review(3, 135.0, 6, note="30d sold")
        call = [c for c in _calls(mock_client) if c[1] == "action_record_review"][0]
        assert call[2] == [[3], 135.0, 6]
        assert call[3] == {"note": "30d sold"}
        assert "review fair" in out["summary"]

    def test_accept_with_buyer_message_sends_both(self, offers, mock_client):
        r = Router({("ebay.best.offer", "read"): [OFFER],
                    ("ebay.best.offer", "action_accept"): True,
                    ("ebay.best.offer", "action_message_buyer"): True,
                    ("product.template", "read"): [PRODUCT]})
        mock_client._models.execute_kw.side_effect = r
        out = offers.accept_offer(3, buyer_message="Deal — thanks, shipping tomorrow.")
        methods = [(c[1], c[2]) for c in _calls(mock_client)]
        assert ("action_accept", [[3]]) in methods
        assert ("action_message_buyer", [[3], "Deal — thanks, shipping tomorrow."]) in methods
        assert out["buyer_message_status"] == "sent"
        assert out["summary"].startswith("ACCEPTED")

    def test_buyer_message_failure_does_not_hide_the_response(self, offers, mock_client):
        def boom(args, kw):
            from xmlrpc.client import Fault
            raise Fault(1, "eBay: member messaging disabled")
        r = Router({("ebay.best.offer", "read"): [OFFER],
                    ("ebay.best.offer", "action_decline"): True,
                    ("ebay.best.offer", "action_message_buyer"): boom,
                    ("product.template", "read"): [PRODUCT]})
        mock_client._models.execute_kw.side_effect = r
        out = offers.decline_offer(3, message="Too low", buyer_message="Sorry, can't go that low.")
        decline = [c for c in _calls(mock_client) if c[1] == "action_decline"][0]
        assert decline[3] == {"message": "Too low"}
        assert out["buyer_message_status"].startswith("FAILED")

    def test_counter_guards_and_payload(self, offers, mock_client):
        r = Router({("ebay.best.offer", "read"): [OFFER],
                    ("ebay.best.offer", "action_counter"): True,
                    ("product.template", "read"): [PRODUCT]})
        mock_client._models.execute_kw.side_effect = r
        with pytest.raises(OdooError, match="above"):
            offers.counter_offer(3, 120)
        with pytest.raises(OdooError, match="below"):
            offers.counter_offer(3, 199.99)
        assert "action_counter" not in [c[1] for c in _calls(mock_client)]
        offers.counter_offer(3, 150, message="Meet in the middle?")
        call = [c for c in _calls(mock_client) if c[1] == "action_counter"][0]
        assert call[2] == [[3], 150.0]
        assert call[3] == {"message": "Meet in the middle?"}

    def test_respond_refuses_closed_or_expired(self, offers, mock_client):
        mock_client._models.execute_kw.side_effect = Router(
            {("ebay.best.offer", "read"): [dict(OFFER, status="declined")]})
        with pytest.raises(OdooError, match="only open"):
            offers.accept_offer(3)
        mock_client._models.execute_kw.side_effect = Router(
            {("ebay.best.offer", "read"): [dict(OFFER, is_expired=True)]})
        with pytest.raises(OdooError, match="expired"):
            offers.accept_offer(3)
        assert "action_accept" not in [c[1] for c in _calls(mock_client)]

    def test_sync_offers_reports_server_message(self, offers, mock_client):
        r = Router({("ebay.best.offer", "action_sync_now"): {"params": {"message": "1 new, 0 updated, 0 closed."}},
                    ("ebay.best.offer", "open_offers_summary"): []})
        mock_client._models.execute_kw.side_effect = r
        out = offers.sync_offers()
        assert out["summary"] == "1 new, 0 updated, 0 closed."

    def test_large_best_offer_id_skips_odoo_id_lookup(self, offers, mock_client):
        """12-digit BestOfferIDs overflow XML-RPC ints; they must go straight to search."""
        r = Router({("ebay.best.offer", "read"): [OFFER],
                    ("ebay.best.offer", "search_read"): [OFFER],
                    ("product.template", "read"): [PRODUCT]})
        mock_client._models.execute_kw.side_effect = r
        out = offers.offer("123456789012")
        assert out["id"] == 3
        reads = [c for c in _calls(mock_client) if c[0] == "ebay.best.offer" and c[1] == "read"]
        assert reads == [] or all(c[2][0] == [3] for c in reads)
        assert any(c[1] == "search_read" and ["best_offer_id", "=", "123456789012"] in c[2][0]
                   for c in _calls(mock_client))

    def test_resolve_propagates_non_missing_errors(self, offers, mock_client):
        from xmlrpc.client import Fault
        def denied(args, kw):
            raise Fault(1, "odoo.exceptions.AccessError: not allowed")
        mock_client._models.execute_kw.side_effect = Router({("ebay.best.offer", "read"): denied})
        with pytest.raises(OdooError, match="AccessError|not allowed"):
            offers.offer(3)
        assert "search_read" not in [c[1] for c in _calls(mock_client)]

    def test_record_review_with_prices_updates_product_comps(self, offers, mock_client):
        r = Router({("ebay.best.offer", "read"): [dict(OFFER, review_verdict="fair", review_median=135.0, review_n=5)],
                    ("ebay.best.offer", "action_record_review"): {"review_verdict": "fair"},
                    ("product.template", "read"): [PRODUCT],
                    ("product.template", "write"): True})
        mock_client._models.execute_kw.side_effect = r
        out = offers.record_review(3, 135.0, 5, prices=[120, 130, 135, 140, 150])
        write = [c for c in _calls(mock_client) if c[0] == "product.template" and c[1] == "write"][0]
        assert write[2][0] == [OFFER["product_tmpl_id"][0]]
        vals = write[2][1]
        assert vals["ebay_comp_median"] == 135.0 and vals["ebay_comp_count"] == 5
        assert "offer_review" in vals["ebay_comp_json"]
        assert out["comps"]["written"] is True

    def test_record_review_without_prices_leaves_product_alone(self, offers, mock_client):
        r = Router({("ebay.best.offer", "read"): [OFFER],
                    ("ebay.best.offer", "action_record_review"): {},
                    ("product.template", "read"): [PRODUCT]})
        mock_client._models.execute_kw.side_effect = r
        offers.record_review(3, 135.0, 5)
        assert ("product.template", "write") not in [(c[0], c[1]) for c in _calls(mock_client)]

    def test_readback_failure_after_response_is_not_a_failure(self, offers, mock_client):
        from xmlrpc.client import Fault
        state = {"reads": 0}
        def read(args, kw):
            state["reads"] += 1
            if state["reads"] > 1:
                raise Fault(1, "database gone away")
            return [OFFER]
        r = Router({("ebay.best.offer", "read"): read,
                    ("ebay.best.offer", "action_accept"): True,
                    ("ebay.best.offer", "action_message_buyer"): True})
        mock_client._models.execute_kw.side_effect = r
        out = offers.accept_offer(3, buyer_message="Deal.")
        assert out["response"] == "accepted"
        assert out["buyer_message_status"] == "sent"
        assert "readback failed" in out["summary"] and out["summary"].startswith("ACCEPTED")

    def test_sync_offers_passes_empty_ids_for_record_method(self, offers, mock_client):
        r = Router({("ebay.best.offer", "action_sync_now"): {"params": {"message": "ok"}},
                    ("ebay.best.offer", "open_offers_summary"): []})
        mock_client._models.execute_kw.side_effect = r
        offers.sync_offers()
        call = [c for c in _calls(mock_client) if c[1] == "action_sync_now"][0]
        assert call[2] == [[]]


class TestPromotionTypeVals:

    def test_coupon_uses_order_benefit_fields(self):
        vals = EbayPromotionOps._type_vals("CODED_COUPON", coupon_code="SAVE10NOW", markdown_percent=10)
        assert vals["coupon_code"] == "SAVE10NOW"
        assert vals["order_benefit_kind"] == "percent" and vals["order_percent"] == 10.0
        assert "markdown_percent" not in vals
        assert vals["order_mode"] == "spend" and vals["order_threshold_amount"] == 0.0

    def test_order_discount_infers_mode(self):
        v = EbayPromotionOps._type_vals("ORDER_DISCOUNT", order_threshold_amount=100, order_percent=10)
        assert v["order_mode"] == "spend"
        v = EbayPromotionOps._type_vals("ORDER_DISCOUNT", order_threshold_qty=2, order_amount=5)
        assert v["order_mode"] == "quantity" and v["order_benefit_kind"] == "amount"
        v = EbayPromotionOps._type_vals("ORDER_DISCOUNT", bogo_buy_qty=2, bogo_get_qty=1, bogo_percent=50)
        assert v["order_mode"] == "bogo" and v["bogo_percent"] == 50.0
        with pytest.raises(OdooError):
            EbayPromotionOps._type_vals("ORDER_DISCOUNT", order_threshold_amount=100)
