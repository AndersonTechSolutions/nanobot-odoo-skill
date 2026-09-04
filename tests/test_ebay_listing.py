"""
Tests for ``EbayListingOps`` — the product.template-based listing lifecycle.

The fork's ``ebay.listing`` model is unused in production; every listing is a
``product.template``. These tests pin the contract that matters for an
unattended agent:

* ``resolve_item`` never treats a bare integer as a product id;
* ``stage_listing`` prepares and reports but never pushes;
* ``publish`` refuses on blockers / already-listed and only then pushes;
* ``set_category_defaults`` never clobbers a hand-set policy;
* ``set_sold_comps`` writes aggregates only, never the computed suggestion.

The mock routes ``execute_kw`` by (model, method) so a test states what each
RPC returns without depending on call order.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from odoo_skill.models._base import OdooActionNotAllowedError  # noqa: E402
from odoo_skill.models.ebay_listing import (  # noqa: E402
    EbayListingOps, _text_to_html,
)
from tests.test_new_connectors import _calls, _ready  # noqa: E402


@pytest.fixture()
def ops(mock_client):
    return _ready(EbayListingOps, mock_client)


class Router:
    """execute_kw side effect keyed by ``(model, method)``.

    Values are either a static return or a callable ``(args, kwargs) -> value``.
    Unrouted calls return ``True`` (Odoo's usual write/button reply).
    """

    def __init__(self, mock_client, routes=None):
        self.routes = dict(routes or {})
        mock_client._models.execute_kw.side_effect = self

    def __call__(self, db, uid, key, model, method, args, kwargs=None):
        handler = self.routes.get((model, method))
        if callable(handler):
            return handler(args, kwargs or {})
        if handler is not None:
            return handler
        return True


def _by(mock_client, model, method):
    return [c for c in _calls(mock_client) if c[0] == model and c[1] == method]


TMPL = "product.template"
READY = {"can_push": True, "blockers": [], "warnings": [], "already_listed": False}


def _state(readiness=None, product=None):
    return {"product": product or {"id": 7, "name": "Dell Optiplex"},
            "readiness": readiness or dict(READY)}


# ── resolve_item ─────────────────────────────────────────────────────


class TestResolveItem:

    def test_bare_integer_is_an_fb_listing_id_not_a_product_id(self, ops, mock_client):
        Router(mock_client, {
            ("fb.marketplace.listing", "search_read"): [
                {"id": 12, "name": "Monitor", "product_tmpl_id": [77, "Monitor"],
                 "state": "listed", "condition": "good"}],
        })
        out = ops.resolve_item("12")
        assert out["kind"] == "fb"
        assert out["fb_listing_id"] == 12
        assert out["product_tmpl_id"] == 77
        assert not _by(mock_client, TMPL, "search_read")

    def test_python_int_is_accepted(self, ops, mock_client):
        Router(mock_client, {("fb.marketplace.listing", "search_read"): []})
        assert ops.resolve_item(12)["fb_listing_id"] == 12

    def test_sku_prefix_needs_a_separator(self, ops, mock_client):
        """``SKU123`` is a SKU named SKU123, not FB listing / SKU ``123``."""
        Router(mock_client, {(TMPL, "search_read"): []})
        ops.resolve_item("SKU123")
        domain = _by(mock_client, TMPL, "search_read")[0][2][0]
        assert domain == [["default_code", "=ilike", "SKU123"]]
        assert not _by(mock_client, "fb.marketplace.listing", "search_read")

    def test_sku_wildcards_are_escaped(self, ops, mock_client):
        Router(mock_client, {(TMPL, "search_read"): []})
        ops.resolve_item("sku FBM_1")
        domain = _by(mock_client, TMPL, "search_read")[0][2][0]
        assert domain == [["default_code", "=ilike", "FBM\\_1"]]

    @pytest.mark.parametrize("ref", ["fb 12", "fb:12", "FB#12"])
    def test_fb_prefixes(self, ops, mock_client, ref):
        Router(mock_client, {("fb.marketplace.listing", "search_read"): []})
        out = ops.resolve_item(ref)
        assert out["kind"] == "fb" and out["fb_listing_id"] == 12
        assert out["product_tmpl_id"] is None
        assert "No FB Marketplace listing #12" in out["summary"]

    def test_sku_exact_match(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "search_read"): [{"id": 5, "name": "Thing", "default_code": "FBM-00012"}],
        })
        out = ops.resolve_item("FBM-00012")
        assert out["kind"] == "sku" and out["product_tmpl_id"] == 5
        domain = _by(mock_client, TMPL, "search_read")[0][2][0]
        assert domain == [["default_code", "=ilike", "FBM-00012"]]

    def test_sku_falls_through_to_name_search_when_no_code_matches(self, ops, mock_client):
        def search_read(args, kw):
            domain = args[0]
            if domain[0][0] == "default_code":
                return []
            return [{"id": 9, "name": "ABC-1 widget"}]
        Router(mock_client, {(TMPL, "search_read"): search_read})
        out = ops.resolve_item("ABC-1")
        assert out["kind"] == "name" and out["product_tmpl_id"] == 9

    def test_name_search_with_many_hits_returns_candidates_only(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "search_read"): [{"id": 1, "name": "Dell A"}, {"id": 2, "name": "Dell B"}],
        })
        out = ops.resolve_item("dell laptop")
        assert out["product_tmpl_id"] is None
        assert [c["id"] for c in out["candidates"]] == [1, 2]
        assert "pick one" in out["summary"]

    def test_name_search_miss(self, ops, mock_client):
        Router(mock_client, {(TMPL, "search_read"): []})
        out = ops.resolve_item("nothing here")
        assert out["product_tmpl_id"] is None and out["candidates"] == []


# ── staging ──────────────────────────────────────────────────────────


class TestSetListingFields:

    def test_title_clipped_to_80_and_description_written_directly(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_save"): _state()})
        ops.set_listing_fields(7, {"ebay_title": "x" * 100}, description="Hello\nworld\n\nBye & hi")
        save = _by(mock_client, TMPL, "ebay_wizard_save")[0]
        assert save[2][0] == [7]
        assert len(save[2][1]["ebay_title"]) == 80
        write = _by(mock_client, TMPL, "write")[0]
        assert write[2][1] == {"ebay_description": "<p>Hello<br/>world</p><p>Bye &amp; hi</p>"}

    def test_html_description_passes_through(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_save"): _state()})
        ops.set_listing_fields(7, {}, description="<p>already html</p>")
        assert _by(mock_client, TMPL, "write")[0][2][1]["ebay_description"] == "<p>already html</p>"

    def test_no_description_means_no_direct_write(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_save"): _state()})
        ops.set_listing_fields(7, {"ebay_fixed_price": 10})
        assert not _by(mock_client, TMPL, "write")


def test_text_to_html_escapes():
    assert _text_to_html("<b>x</b>") == "<p>&lt;b&gt;x&lt;/b&gt;</p>"


POLICY_FIELDS = (
    "ebay_category_id", "ebay_store_category_id", "ebay_template_id",
    "ebay_seller_payment_policy_id", "ebay_seller_return_policy_id",
    "ebay_seller_shipping_policy_id")
BLANK = {f: False for f in POLICY_FIELDS}


class TestSetCategoryDefaults:

    def test_only_blank_fields_are_filled_in_one_write(self, ops, mock_client):
        def read(args, kw):
            return [dict(BLANK, id=7, categ_id=[67, "One-Off"],
                         ebay_seller_shipping_policy_id=[86, "USPS"])]
        cat = dict(BLANK, id=67, ebay_seller_shipping_policy_id=[122, "UPS"],
                   ebay_seller_payment_policy_id=[2, "Pay"],
                   ebay_seller_return_policy_id=[6, "R"], ebay_template_id=[52, "T"])
        Router(mock_client, {(TMPL, "read"): read, ("product.category", "read"): [cat]})
        out = ops.set_category_defaults(7, categ_id=67)
        writes = _by(mock_client, TMPL, "write")
        assert writes[0][2] == [[7], {"categ_id": 67}]
        assert writes[1][2] == [[7], {"ebay_seller_payment_policy_id": 2,
                                      "ebay_seller_return_policy_id": 6,
                                      "ebay_template_id": 52}]
        assert len(writes) == 2
        assert out["ebay_seller_shipping_policy_id"] == 86  # hand-set, kept
        assert out["ebay_seller_payment_policy_id"] == 2
        assert not _by(mock_client, TMPL, "apply_ebay_policies_from_category")

    def test_category_without_defaults_writes_nothing(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "read"): [dict(BLANK, id=7, categ_id=[1, "All"])],
            ("product.category", "read"): [dict(BLANK, id=1)],
        })
        ops.set_category_defaults(7)
        assert not _by(mock_client, TMPL, "write")

    def test_missing_custom_module_is_tolerated(self, ops, mock_client):
        from odoo_skill.errors import OdooError

        def boom(args, kw):
            raise OdooError("Invalid field ebay_template_id on product.category")
        Router(mock_client, {
            (TMPL, "read"): [dict(BLANK, id=7, categ_id=[1, "All"])],
            ("product.category", "read"): boom,
        })
        out = ops.set_category_defaults(7)
        assert out["ebay_template_id"] is False
        assert not _by(mock_client, TMPL, "write")


class TestAddImagesFromFb:

    def test_skips_when_gallery_already_populated(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [{"id": 7, "product_image_ids": [1, 2]}]})
        out = ops.add_images_from_fb(7, 12)
        assert out["skipped"] and out["copied"] == 0
        assert not _by(mock_client, TMPL, "ebay_wizard_add_images")

    def test_copies_fb_photos_in_sequence(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "read"): [{"id": 7, "product_image_ids": []}],
            ("fb.marketplace.listing.image", "search_read"): [
                {"id": 1, "name": "front", "sequence": 1, "image": "AAA="},
                {"id": 2, "name": False, "sequence": 2, "image": "BBB="},
                {"id": 3, "name": "x", "sequence": 3, "image": False},
            ],
        })
        out = ops.add_images_from_fb(7, 12)
        assert out["copied"] == 2
        payload = _by(mock_client, TMPL, "ebay_wizard_add_images")[0][2][1]
        assert payload == [{"datas": "AAA=", "name": "front"},
                           {"datas": "BBB=", "name": "FB photo 2"}]


def _stage_router(mock_client, tmpl, fb=None, conditions=None, state=None,
                  variant_ids=None):
    """Router for the stage_listing flow with a blank policy set."""
    def read(args, kw):
        fields = kw.get("fields") or []
        if "product_image_ids" in fields and len(fields) == 1:
            return [{"id": tmpl["id"], "product_image_ids": tmpl.get("product_image_ids", [])}]
        if set(fields) == {"categ_id", *POLICY_FIELDS}:
            return [dict(BLANK, id=tmpl["id"], categ_id=[1, "All"])]
        return [dict(tmpl)]

    def fb_search(args, kw):
        return [fb] if fb else []

    def cond_search(args, kw):
        code = args[0][0][2]
        return [{"id": (conditions or {}).get(code)}] if (conditions or {}).get(code) else []

    return Router(mock_client, {
        (TMPL, "read"): read,
        ("product.category", "read"): [dict(BLANK, id=1)],
        (TMPL, "search"): lambda a, k: variant_ids or [],
        ("product.product", "search"): lambda a, k: variant_ids or [],
        (TMPL, "ebay_wizard_save"): lambda a, k: dict(state or _state()),
        (TMPL, "ebay_wizard_state"): lambda a, k: dict(state or _state()),
        ("fb.marketplace.listing", "search_read"): fb_search,
        ("ebay.item.condition", "search_read"): cond_search,
        ("fb.marketplace.listing.image", "search_read"): [
            {"id": 1, "name": "a", "sequence": 1, "image": "AAA="}],
    })


BASE_TMPL = {
    "id": 7, "name": "Dell Optiplex 7050", "type": "product", "sale_ok": False,
    "ebay_use": False, "ebay_listing_status": "Unlisted", "list_price": 149.0,
    "ebay_title": False, "ebay_fixed_price": 0.0, "ebay_listing_type": False,
    "ebay_listing_duration": False, "ebay_item_condition_id": False,
    "ebay_condition_description": False, "virtual_available": 3.0,
    "qty_available": 3.0, "product_image_ids": [],
}


class TestStageListing:

    def test_never_pushes(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        ops.stage_listing(7)
        methods = {c[1] for c in _calls(mock_client)}
        assert "ebay_wizard_push" not in methods
        assert "push_product_ebay" not in methods
        assert "action_list_on_ebay" not in methods  # posts chatter; we replicate it

    def test_enables_ebay_and_sale_ok_and_variants(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL, variant_ids=[70, 71])
        out = ops.stage_listing(7)
        writes = _by(mock_client, TMPL, "write")
        assert writes[0][2] == [[7], {"ebay_use": True, "sale_ok": True}]
        vwrite = _by(mock_client, "product.product", "write")[0]
        assert vwrite[2] == [[70, 71], {"ebay_use": True}]
        assert out["staged"]["variant_ebay_use"] == 2

    def test_defaults_and_stock_sync(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        out = ops.stage_listing(7)
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_listing_type"] == "FixedPriceItem"
        assert vals["ebay_listing_duration"] == "GTC"
        assert vals["ebay_title"] == "Dell Optiplex 7050"
        assert vals["ebay_fixed_price"] == 149.0
        assert vals["ebay_best_offer"] is True
        assert vals["ebay_sync_stock"] is True
        assert vals["ebay_quantity"] == 3
        assert out["readiness"]["can_push"] is True
        assert "ready to publish" in out["summary"]

    def test_zero_stock_sets_quantity_zero_so_readiness_blocks(self, ops, mock_client):
        _stage_router(mock_client, dict(BASE_TMPL, virtual_available=0.0, qty_available=2.0))
        out = ops.stage_listing(7)
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_quantity"] == 0
        assert any("quantity set to 0" in n for n in out["notes"])

    def test_negative_forecast_clamps_to_zero(self, ops, mock_client):
        _stage_router(mock_client, dict(BASE_TMPL, virtual_available=-2.0))
        ops.stage_listing(7)
        assert _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]["ebay_quantity"] == 0

    def test_consumable_gets_no_stock_sync(self, ops, mock_client):
        _stage_router(mock_client, dict(BASE_TMPL, type="consu"))
        out = ops.stage_listing(7)
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert "ebay_sync_stock" not in vals and "ebay_quantity" not in vals
        assert any("Not a storable" in n for n in out["notes"])

    def test_fallback_policies_fill_only_blank_fields(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        out = ops.stage_listing(7, fallback={
            "payment_policy_id": 2, "return_policy_id": 6,
            "shipping_policy_id": 122, "template_id": 52})
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_seller_payment_policy_id"] == 2
        assert vals["ebay_seller_return_policy_id"] == 6
        assert vals["ebay_seller_shipping_policy_id"] == 122
        assert vals["ebay_template_id"] == 52
        assert any("Fallback policies" in n for n in out["notes"])

    def test_caller_vals_win_over_defaults(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        ops.stage_listing(7, vals={"ebay_title": "Custom", "ebay_fixed_price": 99.0,
                                   "ebay_best_offer": False})
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_title"] == "Custom"
        assert vals["ebay_fixed_price"] == 99.0
        assert vals["ebay_best_offer"] is False

    def test_fb_listing_supplies_condition_description_title_and_photos(self, ops, mock_client):
        fb = {"id": 12, "name": "Optiplex - like new", "description": "Runs great\n\nNo box",
              "condition": "like_new", "price": 129.0, "product_tmpl_id": [7, "Dell"]}
        _stage_router(mock_client, BASE_TMPL, fb=fb, conditions={"3000": 2})
        out = ops.stage_listing(7, fb_listing_id=12)
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_item_condition_id"] == 2
        assert vals["ebay_condition_description"].startswith("Like new")
        assert vals["ebay_title"] == "Optiplex - like new"
        assert vals["ebay_fixed_price"] == 129.0
        desc = _by(mock_client, TMPL, "write")[-1][2][1]["ebay_description"]
        assert desc == "<p>Runs great</p><p>No box</p>"
        assert _by(mock_client, TMPL, "ebay_wizard_add_images")
        assert any("Copied 1 photo" in n for n in out["notes"])

    def test_fb_listing_of_another_product_is_ignored(self, ops, mock_client):
        fb = {"id": 12, "name": "Other", "description": "x", "condition": "new",
              "price": 1.0, "product_tmpl_id": [99, "Other"]}
        _stage_router(mock_client, BASE_TMPL, fb=fb, conditions={"1000": 1})
        out = ops.stage_listing(7, fb_listing_id=12)
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_title"] == "Dell Optiplex 7050"
        assert "ebay_item_condition_id" not in vals
        assert not _by(mock_client, TMPL, "ebay_wizard_add_images")
        assert any("another product" in n for n in out["notes"])

    def test_already_live_product_is_left_alone(self, ops, mock_client):
        _stage_router(mock_client, dict(BASE_TMPL, ebay_listing_status="Active"))
        out = ops.stage_listing(7)
        assert out["staged"] == {}
        assert not _by(mock_client, TMPL, "write")
        assert not _by(mock_client, TMPL, "ebay_wizard_save")

    def test_existing_values_are_not_overwritten(self, ops, mock_client):
        tmpl = dict(BASE_TMPL, ebay_use=True, sale_ok=True, ebay_title="Keep me",
                    ebay_fixed_price=200.0, ebay_listing_type="FixedPriceItem",
                    ebay_listing_duration="GTC")
        _stage_router(mock_client, tmpl)
        ops.stage_listing(7)
        assert not [w for w in _by(mock_client, TMPL, "write") if "ebay_use" in w[2][1]]
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        for f in ("ebay_title", "ebay_fixed_price", "ebay_listing_type", "ebay_listing_duration"):
            assert f not in vals


# ── publish / end ────────────────────────────────────────────────────


class TestPublish:

    def test_refuses_on_blockers_without_pushing(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "ebay_wizard_state"): _state(
                {"can_push": False, "blockers": ["No photos"], "warnings": [],
                 "already_listed": False}),
        })
        out = ops.publish(7)
        assert out["published"] is False and out["reason"] == "not_ready"
        assert out["blockers"] == ["No photos"]
        assert not _by(mock_client, TMPL, "ebay_wizard_push")

    def test_refuses_when_already_listed(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "ebay_wizard_state"): _state(
                {"can_push": False, "blockers": [], "warnings": [], "already_listed": True}),
            (TMPL, "read"): [{"id": 7, "ebay_url": "https://ebay.com/itm/1",
                              "ebay_listing_status": "Active"}],
        })
        out = ops.publish(7)
        assert out["published"] is False and out["reason"] == "already_listed"
        assert out["ebay_url"] == "https://ebay.com/itm/1"
        assert not _by(mock_client, TMPL, "ebay_wizard_push")

    def test_pushes_when_ready(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "ebay_wizard_state"): _state(),
            (TMPL, "ebay_wizard_push"): {"success": True, "ebay_url": "https://ebay.com/itm/2",
                                         "ebay_listing_status": "Active"},
        })
        out = ops.publish(7)
        assert out["published"] is True
        assert out["ebay_url"] == "https://ebay.com/itm/2"
        assert _by(mock_client, TMPL, "ebay_wizard_push")[0][2] == [[7]]

    def test_push_failure_is_reported_not_raised(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "ebay_wizard_state"): _state(),
            (TMPL, "ebay_wizard_push"): {"success": False, "error": "eBay said no"},
        })
        out = ops.publish(7)
        assert out["published"] is False and out["reason"] == "eBay said no"


class TestEndListing:

    def test_uses_allowlisted_button(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "read"): [{"id": 7, "name": "Dell", "ebay_listing_status": "Ended"}],
        })
        out = ops.end_listing(7)
        assert _by(mock_client, TMPL, "action_end_single_listing")[0][2] == [[7]]
        assert out["status"] == "Ended"

    def test_run_product_action_rejects_unlisted_methods(self, ops):
        with pytest.raises(OdooActionNotAllowedError):
            ops.run_product_action(7, "unlink")
        with pytest.raises(OdooActionNotAllowedError):
            ops.run_product_action(7, "action_list_on_ebay")


# ── comps ────────────────────────────────────────────────────────────


class TestSetSoldComps:

    def test_refuses_fewer_than_three_prices(self, ops, mock_client):
        Router(mock_client)
        out = ops.set_sold_comps(7, [10, 20])
        assert out["written"] is False
        assert not _by(mock_client, TMPL, "write")

    def test_writes_aggregates_never_the_computed_suggestion(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "read"): [{"id": 7, "name": "Dell", "ebay_fixed_price": 100.0,
                              "ebay_comp_count": 5, "ebay_comp_low": 60.0,
                              "ebay_comp_p25": 70.0, "ebay_comp_median": 80.0,
                              "ebay_suggested_price": 85.0,
                              "ebay_suggested_discount_pct": 15.0, "ebay_comp_note": "ok"}],
        })
        out = ops.set_sold_comps(7, [80, 60, 100, 70, 90], listings=[{"title": "x"}])
        vals = _by(mock_client, TMPL, "write")[0][2][1]
        assert vals["ebay_comp_low"] == 60.0 and vals["ebay_comp_high"] == 100.0
        assert vals["ebay_comp_median"] == 80.0
        assert vals["ebay_comp_count"] == 5
        assert "ebay_suggested_price" not in vals
        assert "ebay_fixed_price" not in vals
        blob = json.loads(vals["ebay_comp_json"])
        assert blob["source"] == "ebay_sold_browser" and blob["prices"] == [60, 70, 80, 90, 100]
        assert out["written"] is True and out["actionable"] is True
        assert "5 sold comps" in out["summary"]

    def test_drops_zero_and_negative_prices(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [{"id": 7, "name": "D"}]})
        ops.set_sold_comps(7, [0, -5, 10, 20, 30])
        assert _by(mock_client, TMPL, "write")[0][2][1]["ebay_comp_count"] == 3


class TestReads:

    def test_active_listings_domain(self, ops, mock_client):
        Router(mock_client, {(TMPL, "search_read"): []})
        ops.active_listings()
        domain = _by(mock_client, TMPL, "search_read")[0][2][0]
        assert domain == [["ebay_listing_status", "in", ["Active", "Out Of Stock"]]]

    def test_search_category_leaf_primary_only(self, ops, mock_client):
        Router(mock_client, {("ebay.category", "search_read"): []})
        ops.search_category("desktops")
        domain = _by(mock_client, "ebay.category", "search_read")[0][2][0]
        assert ["category_type", "=", "ebay"] in domain
        assert ["leaf_category", "=", True] in domain
        assert ["name", "ilike", "desktops"] in domain

    def test_readiness_summary(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_state"): _state(
            {"can_push": False, "blockers": ["a", "b"], "warnings": [], "already_listed": False})})
        assert ops.readiness(7)["summary"] == "Blocked: a; b"
