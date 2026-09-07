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
from odoo_skill.errors import OdooError  # noqa: E402
from odoo_skill.models.ebay_listing import (  # noqa: E402
    EbayListingOps, _parse_dims, _parse_package_text, _strip_fb_pickup,
    _text_to_html,
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


def _saves(mock_client):
    """vals of every ``ebay_wizard_save``, in order."""
    return [c[2][1] for c in _by(mock_client, TMPL, "ebay_wizard_save")]


def _saved(mock_client):
    """All wizard saves merged (later saves win)."""
    out = {}
    for vals in _saves(mock_client):
        out.update(vals)
    return out


def _idx(mock_client, method, **field):
    """Index of the first call of *method* (optionally the first ``write``
    / save whose vals hold *field*) in the call list."""
    calls = _calls(mock_client)
    for i, c in enumerate(calls):
        if c[1] != method:
            continue
        if field and not all(c[2][1].get(k) == v for k, v in field.items()):
            continue
        return i
    raise AssertionError(f"no call {method} {field}")


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
        # the fallback is a second save AFTER the resolver ran
        first, fill = _saves(mock_client)
        assert "ebay_seller_payment_policy_id" not in first
        assert fill == {"ebay_seller_payment_policy_id": 2, "ebay_seller_return_policy_id": 6,
                        "ebay_seller_shipping_policy_id": 122, "ebay_template_id": 52}
        assert _idx(mock_client, "ebay_apply_resolved_policies") \
            < _idx(mock_client, "ebay_wizard_save", ebay_template_id=52)
        assert any("Fallback policies" in n for n in out["notes"])

    def test_caller_vals_win_over_defaults(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        ops.stage_listing(7, vals={"ebay_title": "Custom", "ebay_fixed_price": 99.0,
                                   "ebay_best_offer": False})
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_title"] == "Custom"
        assert vals["ebay_fixed_price"] == 99.0
        assert vals["ebay_best_offer"] is False

    def test_operator_quantity_matching_stock_keeps_sync_on(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        out = ops.stage_listing(7, vals={"ebay_quantity": 3})
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_sync_stock"] is True
        assert vals["ebay_quantity"] == 3
        assert not any("sync left OFF" in n for n in out["notes"])

    def test_operator_quantity_differing_from_stock_turns_sync_off(self, ops, mock_client):
        """Q11: sync would overwrite a deliberate partial quantity."""
        _stage_router(mock_client, BASE_TMPL)
        out = ops.stage_listing(7, vals={"ebay_quantity": 1})
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_sync_stock"] is False
        assert vals["ebay_quantity"] == 1
        assert any("sync left OFF" in n for n in out["notes"])

    def test_operator_quantity_string_is_normalised_once(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        ops.stage_listing(7, vals={"ebay_quantity": "3.0"})
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["ebay_quantity"] == 3 and vals["ebay_sync_stock"] is True

    def test_operator_quantity_garbage_is_refused_before_any_write(self, ops, mock_client):
        _stage_router(mock_client, BASE_TMPL)
        with pytest.raises(ValueError, match="whole number"):
            ops.stage_listing(7, vals={"ebay_quantity": "three"})
        assert not _by(mock_client, TMPL, "ebay_wizard_save")

    def test_operator_quantity_on_consumable_adds_no_sync_note(self, ops, mock_client):
        _stage_router(mock_client, dict(BASE_TMPL, type="consu"))
        out = ops.stage_listing(7, vals={"ebay_quantity": 5})
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert "ebay_sync_stock" not in vals
        assert not any("sync left OFF" in n for n in out["notes"])

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


class TestRevise:
    """Stage → approve(hash) → push for a LIVE listing; refusals are dicts."""

    def test_stage_passes_vals_and_html_description(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {
            "success": True, "hash": "abc", "can_revise": True,
            "diff": [{"field": "ebay_title", "label": "Title", "old": "A", "new": "B", "pushable": True},
                     {"field": "ebay_sync_stock", "label": "Sync stock", "old": True, "new": False,
                      "pushable": False}],
            "warnings": []}})
        out = ops.revise_stage(7, {"ebay_title": "  B  ", "ebay_quantity": 2}, description="line1\nline2")
        call = _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0]
        assert call[2][0] == [7]
        assert call[2][1] == {"ebay_title": "B", "ebay_quantity": 2}
        assert call[2][2] == _text_to_html("line1\nline2")
        assert out["hash"] == "abc"
        assert "Revise ready" in out["summary"] and "(Odoo only)" in out["summary"]

    def test_stage_keeps_html_description_verbatim(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {"success": True, "diff": [], "hash": "h"}})
        ops.revise_stage(7, None, description="<p>hi</p>")
        assert _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0][2][2] == "<p>hi</p>"

    def test_stage_without_description_sends_none(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {"success": True, "diff": [], "hash": "h"}})
        out = ops.revise_stage(7, {"ebay_fixed_price": 99.0})
        assert _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0][2][2] is None
        assert out["summary"] == "Nothing changed."

    def test_stage_refresh_description_adds_flag_only_when_set(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {
            "success": True, "hash": "h", "can_revise": True,
            "diff": [{"field": "description", "label": "Description (rendered)",
                      "old": "as published", "new": "rendered 1a2b3c4d", "pushable": True}],
            "warnings": []}})
        out = ops.revise_stage(7, None, refresh_description=True)
        call = _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0]
        assert list(call[2]) == [[7], {}, None, True]
        assert "Description (rendered)" in out["summary"] and "Revise ready" in out["summary"]
        ops.revise_stage(7, None)
        assert len(_by(mock_client, TMPL, "ebay_wizard_revise_stage")[1][2]) == 3

    def test_stage_summary_shows_reason_when_not_pushable(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {
            "success": True, "hash": "h", "can_revise": False,
            "diff": [{"field": "description", "label": "Description (rendered)",
                      "old": "as published", "new": "none", "pushable": False,
                      "reason": "needs both an eBay description and a description template"}],
            "warnings": []}})
        out = ops.revise_stage(7, None, refresh_description=True)
        assert out["summary"].startswith("Nothing pushable")
        assert "will NOT reach eBay: needs both" in out["summary"]

    def test_stage_refusal_is_a_dict(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {
            "success": False, "error": "not_live", "message": "Not live on eBay."}})
        out = ops.revise_stage(7, {"ebay_title": "x"})
        assert out["success"] is False and out["summary"] == "Not live on eBay."

    def test_revise_requires_hash_without_rpc(self, ops, mock_client):
        Router(mock_client)
        out = ops.revise(7, "")
        assert out == {"revised": False, "reason": "hash_required",
                       "summary": "Pass the hash from the staged diff."}
        assert not _by(mock_client, TMPL, "ebay_wizard_revise")

    def test_revise_pushes_with_hash(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise"): {
            "success": True, "ebay_url": "https://www.ebay.com/itm/1", "status": "Active",
            "pushed": ["Title"], "skipped": [], "message": "Revised: Title."}})
        out = ops.revise(7, "abc")
        assert _by(mock_client, TMPL, "ebay_wizard_revise")[0][2] == [[7], "abc"]
        assert out["revised"] is True and out["pushed"] == ["Title"]
        assert out["ebay_url"].endswith("/itm/1") and out["summary"] == "Revised: Title."

    def test_revise_stale_refusal_not_raised(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise"): {
            "success": False, "error": "stale", "message": "Product changed since staging."}})
        out = ops.revise(7, "old")
        assert out["revised"] is False and out["reason"] == "stale"
        assert out["partial_risk"] is False and "changed" in out["summary"]

    def test_revise_ebay_error_carries_partial_risk(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise"): {
            "success": False, "error": "ebay_error", "partial_risk": True,
            "message": "boom — eBay may have been PARTIALLY updated"}})
        out = ops.revise(7, "abc")
        assert out["reason"] == "ebay_error" and out["partial_risk"] is True

    def test_status_is_read_only(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_status"): {
            "success": True, "can_revise": False, "diff": [], "hash": None}})
        out = ops.revision_status(7)
        calls = _by(mock_client, TMPL, "ebay_wizard_revise_status")
        assert len(calls) == 1 and calls[0][2] == [[7]]
        assert out["can_revise"] is False
        assert not _by(mock_client, TMPL, "write")

    def test_discard_summary(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_discard"): {
            "success": True, "restored": ["Title", "Price"], "not_restored": []}})
        out = ops.revise_discard(7)
        calls = _by(mock_client, TMPL, "ebay_wizard_revise_discard")
        assert len(calls) == 1 and calls[0][2] == [[7]]
        assert out["summary"] == "Discarded staged revision (Title, Price restored)."

    def test_discard_nothing_staged(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_discard"): {
            "success": False, "error": "nothing_staged", "message": "No staged revision."}})
        assert ops.revise_discard(7)["summary"] == "No staged revision."


class TestSpecifics:
    """Item specifics: status / category aspects / set (server does the work)."""

    STATUS = {
        "success": True, "source": "ebay", "can_push": False,
        "category": {"id": 5707, "name": "Headsets"},
        "required": [
            {"name": "Type", "status": "placeholder", "value": "TOBEFILLED",
             "attribute_id": 12, "mode": "select",
             "values_sample": ["Headset", "Earpiece"], "values_total": 10},
            {"name": "Brand", "status": "ok", "value": "Bose", "attribute_id": 3,
             "mode": "free", "values_sample": [], "values_total": 0},
            {"name": "Model", "status": "missing", "value": None, "attribute_id": None,
             "mode": "free", "values_sample": [], "values_total": 0},
        ],
        "optional": [
            {"name": "Cup Style", "status": "invalid", "value": "Over Ear",
             "attribute_id": 9, "mode": "select",
             "values_sample": ["Over-Ear", "On-Ear"], "values_total": 2},
        ],
        "extra": ["Colour"],
        "blocking": ["Type", "Model"],
    }

    def test_status_passes_refresh_and_summarises(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_specifics_status"): dict(self.STATUS)})
        out = ops.specifics_status(7, refresh=True)
        calls = _by(mock_client, TMPL, "ebay_wizard_specifics_status")
        assert len(calls) == 1 and calls[0][2] == [[7], True]
        s = out["summary"]
        assert s.startswith("BLOCKED — eBay requires: Type, Model | ")
        assert "Type — PLACEHOLDER (select: Headset, Earpiece …+8)" in s
        assert "Brand=Bose" in s
        assert "Model — MISSING (no Odoo attribute)" in s
        assert "Cup Style=Over Ear — INVALID (allowed: Over-Ear, On-Ear)" in s
        assert "also sent: Colour" in s
        assert not _by(mock_client, TMPL, "write")

    def test_status_ok_default_no_refresh(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_specifics_status"): {
            "success": True, "source": "cache", "can_push": True,
            "required": [{"name": "Type", "status": "ok", "value": "Headset"}],
            "optional": [], "extra": [], "blocking": []}})
        out = ops.specifics_status(7)
        assert _by(mock_client, TMPL, "ebay_wizard_specifics_status")[0][2] == [[7], False]
        assert out["summary"] == "Specifics OK | required: Type=Headset"

    def test_status_source_none_and_no_category(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_specifics_status"): {
            "success": True, "source": "none", "can_push": True,
            "required": [], "optional": [], "extra": [], "blocking": []}})
        s = ops.specifics_status(7)["summary"]
        assert s.startswith("Specifics UNVERIFIED — eBay aspect list unavailable")
        assert "Specifics OK" not in s
        Router(mock_client, {(TMPL, "ebay_wizard_specifics_status"): {
            "success": False, "error": "no_category", "message": "No eBay category set."}})
        assert ops.specifics_status(7)["summary"] == "No eBay category set."
        Router(mock_client, {(TMPL, "ebay_wizard_specifics_status"): {
            "success": False, "error": "access"}})
        assert ops.specifics_status(7)["summary"] == "Refused: access"

    def test_status_multi_value_does_not_invite_guessing(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_specifics_status"): {
            "success": True, "source": "cache", "can_push": False,
            "required": [{"name": "Type", "status": "multi_value", "value": "Headset, Earpiece"}],
            "optional": [], "extra": [], "blocking": ["Type"]}})
        s = ops.specifics_status(7)["summary"]
        assert "Type=Headset, Earpiece — MULTI-VALUE" in s
        assert "pick one" not in s and "ask the operator" in s

    ASPECTS = {
        "success": True, "category": {"id": 5707},
        "aspects": [
            {"name": "Type", "required": True, "usage": "required", "mode": "select",
             "values": [f"V{i}" for i in range(10)]},
            {"name": "Brand", "required": True, "usage": "required", "mode": "free", "values": []},
            {"name": "Model", "required": False, "usage": "recommended", "mode": "free",
             "values": []},
        ],
    }

    def test_category_aspects_summary_truncates(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_category_aspects"): dict(self.ASPECTS)})
        out = ops.category_aspects(7)
        assert _by(mock_client, TMPL, "ebay_wizard_category_aspects")[0][2] == [[7], False]
        assert len(out["aspects"]) == 3
        assert out["summary"] == (
            "Type [REQUIRED, select: V0, V1, V2, V3, V4, V5, V6, V7 …+2]; "
            "Brand [REQUIRED, free text]; Model [recommended, free text]")

    def test_category_aspects_filter_full_list(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_category_aspects"): dict(self.ASPECTS)})
        out = ops.category_aspects(7, refresh=True, aspect="  type ")
        assert _by(mock_client, TMPL, "ebay_wizard_category_aspects")[0][2] == [[7], True]
        assert [a["name"] for a in out["aspects"]] == ["Type"]
        assert out["summary"] == "Type [REQUIRED, select: " + ", ".join(f"V{i}" for i in range(10)) + "]"
        out = ops.category_aspects(7, aspect="Colour")
        assert out["aspects"] == [] and out["summary"] == "No aspect named 'Colour' in this category."

    def test_category_aspects_failure(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_category_aspects"): {
            "success": False, "error": "fetch_failed", "message": "eBay timeout."}})
        assert ops.category_aspects(7)["summary"] == "eBay timeout."

    def test_set_passes_clean_values_and_flags(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_set_specifics"): {
            "success": True, "needs_attributes": [],
            "results": [
                {"name": "Type", "input": "headset", "written": True, "attribute": "existing",
                 "value": "existing", "stored_value": "Headset", "skipped": None},
                {"name": "Brand", "input": "Bose", "written": True, "attribute": "existing",
                 "value": "created", "stored_value": "Bose", "skipped": None},
            ],
            "status": {"success": True, "source": "ebay", "can_push": True,
                       "required": [{"name": "Type", "status": "ok", "value": "Headset"},
                                    {"name": "Brand", "status": "ok", "value": "Bose"}],
                       "optional": [], "extra": [], "blocking": []}}})
        out = ops.set_specifics(7, {" Type ": " headset ", "Brand": "Bose", "Model": None})
        calls = _by(mock_client, TMPL, "ebay_wizard_set_specifics")
        assert len(calls) == 1
        assert calls[0][2] == [[7], {"Type": "headset", "Brand": "Bose", "Model": ""}, False, False]
        assert out["summary"] == ("Type=Headset [wrote]; Brand=Bose (new value) [wrote]"
                                  " | Specifics OK | required: Type=Headset; Brand=Bose")

    def test_set_create_attributes_dry_run_flags(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_set_specifics"): {
            "success": True, "needs_attributes": [],
            "results": [{"name": "Model", "input": "A20", "written": False,
                         "attribute": "created", "value": "created", "stored_value": "A20",
                         "skipped": None}]}})
        out = ops.set_specifics(7, {"Model": "A20"}, create_attributes=True, dry_run="yes")
        assert _by(mock_client, TMPL, "ebay_wizard_set_specifics")[0][2] == \
            [[7], {"Model": "A20"}, True, True]
        assert out["summary"] == "Model=A20 (new attribute, new value) [would write]"

    def test_set_create_attributes_string_false_stays_false(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_set_specifics"): {
            "success": True, "needs_attributes": [], "results": []}})
        for val in ("false", "False", "0", "no", 0, ""):
            ops.set_specifics(7, {"Model": "A20"}, create_attributes=val)
        calls = _by(mock_client, TMPL, "ebay_wizard_set_specifics")
        assert len(calls) == 6 and all(c[2][2] is False for c in calls)
        ops.set_specifics(7, {"Model": "A20"}, create_attributes="true")
        assert _by(mock_client, TMPL, "ebay_wizard_set_specifics")[-1][2][2] is True

    def test_set_create_attributes_garbage_refused(self, ops, mock_client):
        Router(mock_client)
        for val in ("maybe", 2, [True], {"ok": 1}, "ok"):
            out = ops.set_specifics(7, {"Model": "A20"}, create_attributes=val)
            assert out["success"] is False and out["error"] == "bad_create_attributes"
        assert not _by(mock_client, TMPL, "ebay_wizard_set_specifics")

    def test_set_skips_and_needs_attributes(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_set_specifics"): {
            "success": True, "needs_attributes": ["Model"],
            "results": [
                {"name": "Model", "input": "A20", "written": False, "attribute": "missing",
                 "skipped": "attribute_missing"},
                {"name": "Type", "input": "Headphone", "written": False, "attribute": "existing",
                 "skipped": "invalid_value", "suggestions": ["Headset", "Earpiece"]},
                {"name": "Grade", "input": "A", "written": False, "skipped": "variant_attribute"},
                {"name": "Foo", "input": "x", "written": False, "skipped": "not_an_aspect"},
                {"name": "Bar", "input": "y", "written": False, "skipped": "aspects_unavailable"},
                {"name": "Baz", "input": "", "written": False, "skipped": "blank"},
            ],
            "status": {"success": True, "source": "ebay", "can_push": False,
                       "required": [{"name": "Type", "status": "placeholder", "value": "TOBEFILLED",
                                     "attribute_id": 12, "mode": "select",
                                     "values_sample": ["Headset"], "values_total": 1}],
                       "optional": [], "extra": [], "blocking": ["Type"]}}})
        out = ops.set_specifics(7, {"Model": "A20", "Type": "Headphone", "Grade": "A",
                                    "Foo": "x", "Bar": "y", "Baz": ""})
        s = out["summary"]
        assert "Model — NO ODOO ATTRIBUTE (ask Ian; retry with create_attributes)" in s
        assert "Type='Headphone' — INVALID (did you mean: Headset, Earpiece)" in s
        assert "Grade — skipped (variant attribute)" in s
        assert "Foo — skipped (not an aspect of this category)" in s
        assert "Bar — skipped (eBay aspect list unavailable, retry later)" in s
        assert "Baz — skipped (blank)" in s
        assert " | needs OK to create attribute(s): Model | BLOCKED — eBay requires: Type | " in s
        assert s.endswith("required: Type — PLACEHOLDER (select: Headset)")

    def test_set_bad_values_no_rpc(self, ops, mock_client):
        Router(mock_client)
        for bad in ({}, None, "Type=Headset", ["Type"]):
            out = ops.set_specifics(7, bad)
            assert out["success"] is False and out["error"] == "bad_values"
        assert not _by(mock_client, TMPL, "ebay_wizard_set_specifics")

    def test_set_server_refusal(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_set_specifics"): {
            "success": False, "error": "no_category", "message": "No eBay category set."}})
        out = ops.set_specifics(7, {"Type": "Headset"})
        assert out["success"] is False and out["summary"] == "No eBay category set."
        Router(mock_client, {(TMPL, "ebay_wizard_set_specifics"): {
            "success": False, "error": "access"}})
        assert ops.set_specifics(7, {"Type": "Headset"})["summary"] == "Refused: access"



# ── package (weight / dims) + policy resolver ────────────────────────


class TestParsePackageText:

    def test_lb_and_inches(self):
        out = _parse_package_text(["Weight: 12.5 lb. Dimensions: 18 x 12 x 6 in"])
        assert out["weight_lb"] == 12.5
        assert out["dims"] == (18.0, 12.0, 6.0)

    def test_oz_and_lb_oz_and_kg_convert_to_lb(self):
        assert _parse_package_text(["weight 36oz"])["weight_lb"] == 2.25
        assert _parse_package_text(["Weighs 2 lb 4 oz"])["weight_lb"] == 2.25
        assert _parse_package_text(["weight 3.2 kg"])["weight_lb"] == 7.05

    def test_font_weight_css_and_bare_numbers_do_not_match(self):
        html_desc = "<style>p{font-weight:700}</style><p>Ships in a 36oz mug</p>"
        out = _parse_package_text([html_desc])
        assert out["weight_lb"] is None and out["dims"] is None

    def test_resolution_triplet_is_not_a_box_but_unit_or_cue_is(self):
        assert _parse_package_text(["1920 x 1080 x 60Hz"])["dims"] is None
        assert _parse_package_text(['Box 20" × 14" × 8"'])["dims"] == (20.0, 14.0, 8.0)
        assert _parse_package_text(["Package size: 20 x 14 x 8"])["dims"] == (20.0, 14.0, 8.0)

    def test_cm_converts_to_inches(self):
        assert _parse_package_text(["size 40 x 30 x 20 cm"])["dims"] == (15.75, 11.81, 7.87)

    def test_mm_and_uppercase_units_convert(self):
        assert _parse_package_text(["Box: 400 x 300 x 200 mm"])["dims"] == (15.75, 11.81, 7.87)
        assert _parse_package_text(["Size 40 X 30 X 20 CM"])["dims"] == (15.75, 11.81, 7.87)
        assert _parse_package_text(["SIZE 18 IN X 12 IN X 6 IN"])["dims"] == (18.0, 12.0, 6.0)

    def test_mixed_units_convert_per_axis(self):
        assert _parse_package_text(['Dims 18 in x 30 cm x 6"'])["dims"] == (18.0, 11.81, 6.0)
        assert _parse_package_text(["Dims 18 in x 300 mm x 6 cm"])["dims"] == (18.0, 11.81, 2.36)

    def test_unsupported_unit_is_rejected_with_a_warning(self):
        for text in ["Box 2 x 1 x 0.5 m", "Package 6 x 4 x 2 ft", "size 1920 x 1080 x 60Hz"]:
            out = _parse_package_text([text])
            assert out["dims"] is None, text
            assert out["warnings"] and "not in/cm/mm" in out["warnings"][0], text

    def test_word_starting_with_in_is_not_an_inch_unit(self):
        # "inside" is not "in"; without a unit and without a cue there is no box
        assert _parse_package_text(["measures 20 x 14 x 8 inside"])["dims"] is None

    def test_first_text_with_a_value_wins_per_value(self):
        out = _parse_package_text(["weight: 3 lb", "weight: 9 lb, dims 1 x 2 x 3 in"])
        assert out["weight_lb"] == 3.0 and out["dims"] == (1.0, 2.0, 3.0)

    def test_non_strings_are_skipped(self):
        assert _parse_package_text([False, None, ""])["weight_lb"] is None


class TestParseDims:

    @pytest.mark.parametrize("value", ["18x12x6", "18 × 12 × 6 in", '18" x 12" x 6"',
                                       [18, "12", 6.0], (18, 12, 6)])
    def test_accepted_forms(self, value):
        assert _parse_dims(value) == (18.0, 12.0, 6.0)

    @pytest.mark.parametrize("value", ["18x12", "a x b x c", [1, 2], "0x1x2"])
    def test_rejected_forms(self, value):
        with pytest.raises(ValueError, match="dims"):
            _parse_dims(value)

    def test_none_passes_through(self):
        assert _parse_dims(None) is None


class TestStripFbPickup:

    def test_trailing_sentence_pair(self):
        assert _strip_fb_pickup("Runs great.\n\nNo box. Local pickup. Message with any questions.") \
            == "Runs great.\n\nNo box."

    def test_own_line_variants(self):
        assert _strip_fb_pickup("Runs great.\nLocal pickup only.\n") == "Runs great."
        assert _strip_fb_pickup("Runs great. Local pickup! Message me with any questions.") == "Runs great."

    def test_mid_sentence_mention_is_kept(self):
        text = "Local pickup available at our shop in Clearwater. Tested."
        assert _strip_fb_pickup(text) == text

    def test_empty(self):
        assert _strip_fb_pickup("") == ""


PKG_TMPL = dict(BASE_TMPL, weight=0.0, ebay_pkg_length_in=0.0, ebay_pkg_width_in=0.0,
                ebay_pkg_height_in=0.0, description_sale=False, ebay_description=False)


class TestStagePackage:

    def test_caller_weight_and_dims_go_through_the_wizard_save(self, ops, mock_client):
        _stage_router(mock_client, PKG_TMPL)
        out = ops.stage_listing(7, weight_lb=12.5, dims="18x12x6")
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert vals["weight"] == 12.5
        assert (vals["ebay_pkg_length_in"], vals["ebay_pkg_width_in"],
                vals["ebay_pkg_height_in"]) == (18.0, 12.0, 6.0)
        assert out["package"] == {
            "weight_lb": 12.5, "length_in": 18.0, "width_in": 12.0, "height_in": 6.0,
            "source": "field", "weight_source": "field", "dims_source": "field"}
        assert not any("missing" in n for n in out["notes"])

    def test_existing_fields_are_reported_and_not_rewritten(self, ops, mock_client):
        tmpl = dict(PKG_TMPL, weight=4.0, ebay_pkg_length_in=10.0,
                    ebay_pkg_width_in=8.0, ebay_pkg_height_in=6.0)
        _stage_router(mock_client, tmpl)
        out = ops.stage_listing(7)
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert "weight" not in vals and "ebay_pkg_length_in" not in vals
        assert out["package"]["source"] == "field"
        assert out["package"]["weight_lb"] == 4.0

    def test_description_fallback_applies_and_notes(self, ops, mock_client):
        tmpl = dict(PKG_TMPL, description_sale="Weight: 12 lb. Box 18 x 12 x 6 in.")
        _stage_router(mock_client, tmpl)
        out = ops.stage_listing(7)
        first, fill = _saves(mock_client)
        assert "weight" not in first          # main save first, fallback is a second save
        assert fill == {"weight": 12.0, "ebay_pkg_length_in": 18.0,
                        "ebay_pkg_width_in": 12.0, "ebay_pkg_height_in": 6.0}
        assert out["package"]["source"] == "description"
        assert any("taken from the description" in n for n in out["notes"])

    def test_html_ebay_description_and_fb_description_are_searched(self, ops, mock_client):
        fb = {"id": 12, "name": "Optiplex", "description": "Weighs 2 lb 4 oz", "condition": "good",
              "price": 129.0, "product_tmpl_id": [7, "Dell"]}
        tmpl = dict(PKG_TMPL, ebay_description='<p style="font-weight:700">Dims: 20" x 14" x 8"</p>')
        _stage_router(mock_client, tmpl, fb=fb, conditions={"3000": 2})
        out = ops.stage_listing(7, fb_listing_id=12)
        vals = _saved(mock_client)
        assert vals["weight"] == 2.25
        assert vals["ebay_pkg_length_in"] == 20.0
        assert out["package"]["source"] == "description"

    def test_vals_package_keys_beat_kwargs_and_description(self, ops, mock_client):
        tmpl = dict(PKG_TMPL, description_sale="Weight: 12 lb. Box 18 x 12 x 6 in.")
        _stage_router(mock_client, tmpl)
        out = ops.stage_listing(7, weight_lb=5, dims="1x2x3",
                                vals={"weight": 7.0, "ebay_pkg_height_in": 9.0})
        assert len(_saves(mock_client)) == 1          # nothing left for the description
        vals = _saves(mock_client)[0]
        assert vals["weight"] == 7.0
        assert (vals["ebay_pkg_length_in"], vals["ebay_pkg_width_in"],
                vals["ebay_pkg_height_in"]) == (1.0, 2.0, 9.0)
        assert out["package"]["weight_lb"] == 7.0 and out["package"]["height_in"] == 9.0
        assert out["package"]["source"] == "field"
        assert not any("description" in n for n in out["notes"])

    def test_server_package_is_consulted_before_the_description(self, ops, mock_client):
        # WP-A's wizard state carries the legacy-attribute fallback; the text
        # must not override what the server already knows.
        state = dict(_state(), package={"weight_lb": 4.5, "length": 10.0, "width": 8.0,
                                        "height": 6.0, "package_type": "PackageThickEnvelope"})
        tmpl = dict(PKG_TMPL, description_sale="Weight: 12 lb. Box 18 x 12 x 6 in.")
        _stage_router(mock_client, tmpl, state=state)
        out = ops.stage_listing(7)
        assert len(_saves(mock_client)) == 1
        assert "weight" not in _saves(mock_client)[0]
        assert out["package"] == {
            "weight_lb": 4.5, "length": 10.0, "width": 8.0, "height": 6.0,
            "package_type": "PackageThickEnvelope",
            "length_in": 10.0, "width_in": 8.0, "height_in": 6.0,
            "source": "field", "weight_source": "field", "dims_source": "field"}
        assert not any("description" in n or "missing" in n for n in out["notes"])

    def test_partial_dims_fill_only_the_missing_axes(self, ops, mock_client):
        state = dict(_state(), package={"weight_lb": 4.5, "length": 10.0, "width": None,
                                        "height": None, "package_type": False})
        tmpl = dict(PKG_TMPL, description_sale="Box 18 x 12 x 6 in.")
        _stage_router(mock_client, tmpl, state=state)
        out = ops.stage_listing(7)
        first, fill = _saves(mock_client)
        assert fill == {"ebay_pkg_width_in": 12.0, "ebay_pkg_height_in": 6.0}
        assert out["package"]["length_in"] == 10.0
        assert out["package"]["dims_source"] == "description"
        assert out["package"]["package_type"] is False      # server key kept
        assert any("width 12, height 6 in taken from the description" in n
                   for n in out["notes"])

    def test_unsupported_unit_in_description_is_a_warning_not_a_value(self, ops, mock_client):
        tmpl = dict(PKG_TMPL, description_sale="Box 2 x 1 x 0.5 m")
        _stage_router(mock_client, tmpl)
        out = ops.stage_listing(7)
        assert len(_saves(mock_client)) == 1 and "ebay_pkg_length_in" not in _saved(mock_client)
        assert out["package"]["dims_source"] == "missing"
        assert any("not in/cm/mm" in n for n in out["notes"])

    def test_caller_value_beats_description(self, ops, mock_client):
        tmpl = dict(PKG_TMPL, description_sale="Weight: 12 lb")
        _stage_router(mock_client, tmpl)
        out = ops.stage_listing(7, weight_lb=5)
        assert _saved(mock_client)["weight"] == 5.0
        assert len(_saves(mock_client)) == 1
        assert out["package"]["weight_source"] == "field"
        assert out["package"]["dims_source"] == "missing"

    def test_missing_is_a_note_not_a_block(self, ops, mock_client):
        _stage_router(mock_client, PKG_TMPL)
        out = ops.stage_listing(7)
        vals = _by(mock_client, TMPL, "ebay_wizard_save")[0][2][1]
        assert "weight" not in vals
        assert out["package"]["source"] == "missing"
        assert out["package"]["weight_lb"] is None
        assert any("Package weight/dims missing" in n and "--dims LxWxH" in n
                   for n in out["notes"])
        assert out["readiness"]["can_push"] is True   # readiness stays Odoo's

    @pytest.mark.parametrize("kw", [{"weight_lb": 0}, {"weight_lb": "heavy"},
                                    {"dims": "18x12"}, {"dims": "0x1x1"}])
    def test_bad_values_are_refused_before_any_write(self, ops, mock_client, kw):
        _stage_router(mock_client, PKG_TMPL)
        with pytest.raises(ValueError):
            ops.stage_listing(7, **kw)
        assert not _calls(mock_client)

    def test_fb_pickup_trailer_is_stripped_from_ebay_description(self, ops, mock_client):
        fb = {"id": 12, "name": "Optiplex", "condition": "good", "price": 129.0,
              "description": "Runs great.\n\nLocal pickup. Message with any questions.",
              "product_tmpl_id": [7, "Dell"]}
        _stage_router(mock_client, PKG_TMPL, fb=fb, conditions={"3000": 2})
        out = ops.stage_listing(7, fb_listing_id=12)
        desc = [w for w in _by(mock_client, TMPL, "write") if "ebay_description" in w[2][1]][0]
        assert desc[2][1]["ebay_description"] == "<p>Runs great.</p>"
        assert any("Local pickup" in n for n in out["notes"])

    def test_caller_description_is_not_touched(self, ops, mock_client):
        fb = {"id": 12, "name": "Optiplex", "condition": "good", "price": 129.0,
              "description": "x. Local pickup.", "product_tmpl_id": [7, "Dell"]}
        _stage_router(mock_client, PKG_TMPL, fb=fb, conditions={"3000": 2})
        ops.stage_listing(7, fb_listing_id=12, description="Mine. Local pickup.")
        desc = [w for w in _by(mock_client, TMPL, "write") if "ebay_description" in w[2][1]][0]
        assert desc[2][1]["ebay_description"] == "<p>Mine. Local pickup.</p>"


class TestStagePolicies:

    def test_resolver_runs_after_category_copy_and_overrides_are_written_first(self, ops, mock_client):
        _stage_router(mock_client, PKG_TMPL)
        out = ops.stage_listing(7, shipping_mode="calculated", return_mode="none", warranty="1y")
        calls = _calls(mock_client)
        ov = [c for c in calls if c[1] == "write" and "ebay_shipping_mode" in c[2][1]][0]
        assert ov[2][1] == {"ebay_shipping_mode": "calculated", "ebay_return_mode": "none",
                            "ebay_warranty_kind": "months", "ebay_warranty_months": 12}
        idx_ov = calls.index(ov)
        idx_apply = next(i for i, c in enumerate(calls) if c[1] == "ebay_apply_resolved_policies")
        idx_save = next(i for i, c in enumerate(calls) if c[1] == "ebay_wizard_save")
        idx_categ = next(i for i, c in enumerate(calls)
                         if c[1] == "read" and "categ_id" in (c[3].get("fields") or []))
        # category copy → save (condition etc.) → overrides → resolver → refresh
        assert idx_categ < idx_save < idx_ov < idx_apply
        assert calls[idx_apply][2] == [[7]]
        idx_state = next(i for i, c in enumerate(calls) if c[1] == "ebay_wizard_state")
        assert idx_apply < idx_state
        assert out["staged"]["policies_resolved"] is True
        assert out["staged"]["ebay_warranty_months"] == 12

    def test_fb_condition_is_saved_before_the_resolver_runs(self, ops, mock_client):
        fb = {"id": 12, "name": "Optiplex", "condition": "good", "price": 129.0,
              "description": "x", "product_tmpl_id": [7, "Dell"]}
        _stage_router(mock_client, PKG_TMPL, fb=fb, conditions={"3000": 2})
        ops.stage_listing(7, fb_listing_id=12)
        assert _idx(mock_client, "ebay_wizard_save", ebay_item_condition_id=2) \
            < _idx(mock_client, "ebay_apply_resolved_policies")

    def test_explicit_condition_is_saved_before_the_resolver_runs(self, ops, mock_client):
        _stage_router(mock_client, PKG_TMPL)
        ops.stage_listing(7, vals={"ebay_item_condition_id": 5}, warranty="none")
        assert _idx(mock_client, "ebay_wizard_save", ebay_item_condition_id=5) \
            < _idx(mock_client, "write", ebay_warranty_kind="none") \
            < _idx(mock_client, "ebay_apply_resolved_policies")

    def test_state_is_refreshed_after_the_resolver(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)
        router.routes[(TMPL, "ebay_wizard_save")] = lambda a, k: dict(_state(), fresh=False)
        router.routes[(TMPL, "ebay_wizard_state")] = lambda a, k: dict(_state(), fresh=True)
        assert ops.stage_listing(7)["fresh"] is True

    @pytest.mark.parametrize("warranty,kind,months", [
        ("none", "none", 0), ("factory", "factory", 0), ("30d", "months", 1),
        ("2y", "months", 24), ("3Y", "months", 36), ("auto", "auto", 0)])
    def test_warranty_mapping(self, ops, mock_client, warranty, kind, months):
        _stage_router(mock_client, PKG_TMPL)
        out = ops.stage_listing(7, warranty=warranty)
        assert out["staged"]["ebay_warranty_kind"] == kind
        assert out["staged"]["ebay_warranty_months"] == months

    @pytest.mark.parametrize("kw", [{"shipping_mode": "cheap"}, {"return_mode": "maybe"},
                                    {"warranty": "5y"}])
    def test_bad_override_refused_before_any_write(self, ops, mock_client, kw):
        _stage_router(mock_client, PKG_TMPL)
        with pytest.raises(ValueError, match="must be one of"):
            ops.stage_listing(7, **kw)
        assert not _calls(mock_client)

    def test_no_override_means_no_override_write(self, ops, mock_client):
        _stage_router(mock_client, PKG_TMPL)
        ops.stage_listing(7)
        assert not [c for c in _calls(mock_client)
                    if c[1] == "write" and "ebay_shipping_mode" in c[2][1]]
        assert _by(mock_client, TMPL, "ebay_apply_resolved_policies")

    def test_fallback_only_fills_what_the_resolver_left_blank(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)
        base_read = router.routes[(TMPL, "read")]

        def read(args, kw):
            if set(kw.get("fields") or []) == set(POLICY_FIELDS):
                # after the resolver: shipping + return set, payment still blank
                return [dict(BLANK, id=7, ebay_seller_shipping_policy_id=[51, "USPS"],
                             ebay_seller_return_policy_id=[6, "30d"])]
            return base_read(args, kw)
        router.routes[(TMPL, "read")] = read
        out = ops.stage_listing(7, fallback={"payment_policy_id": 2, "return_policy_id": 8,
                                             "shipping_policy_id": 122, "template_id": 52})
        vals = _saved(mock_client)
        assert vals["ebay_seller_payment_policy_id"] == 2
        assert vals["ebay_template_id"] == 52
        assert "ebay_seller_shipping_policy_id" not in vals
        assert "ebay_seller_return_policy_id" not in vals
        note = [n for n in out["notes"] if n.startswith("Fallback policies")][0]
        assert "no default" in note

    def test_missing_resolver_on_old_server_is_a_note(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)

        def apply(args, kw):
            raise OdooError("Odoo error on product.template.ebay_apply_resolved_policies: "
                            "AttributeError: 'product.template' object has no attribute "
                            "'ebay_apply_resolved_policies'")
        router.routes[(TMPL, "ebay_apply_resolved_policies")] = apply
        out = ops.stage_listing(7, fallback={"shipping_policy_id": 122})
        assert any("resolver not available" in n for n in out["notes"])
        assert out["staged"].get("policies_resolved") is None
        assert _saved(mock_client)["ebay_seller_shipping_policy_id"] == 122

    def test_resolver_failure_surfaces(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)

        def apply(args, kw):
            raise OdooError("Odoo error on product.template.ebay_apply_resolved_policies: "
                            "UserError: no return policy configured")
        router.routes[(TMPL, "ebay_apply_resolved_policies")] = apply
        with pytest.raises(OdooError, match="no return policy"):
            ops.stage_listing(7)

    def test_policy_resolution_passes_through_from_wizard_state(self, ops, mock_client):
        resolution = {"shipping_mode": "free", "shipping_policy_id": 51,
                      "return_text": "30-Day Returns", "warranty_label": "30 Day Warranty",
                      "warnings": ["fallback used"]}
        state = dict(_state(), policy_resolution=resolution)
        _stage_router(mock_client, PKG_TMPL, state=state)
        out = ops.stage_listing(7)
        assert out["policy_resolution"] == resolution

    def test_policy_resolution_key_always_present(self, ops, mock_client):
        _stage_router(mock_client, PKG_TMPL)
        assert ops.stage_listing(7)["policy_resolution"] == {}

    def test_live_listing_gets_no_override_write(self, ops, mock_client):
        _stage_router(mock_client, dict(PKG_TMPL, ebay_listing_status="Active"))
        out = ops.stage_listing(7, weight_lb=3, warranty="1y")
        assert not _by(mock_client, TMPL, "write")
        assert not _by(mock_client, TMPL, "ebay_apply_resolved_policies")
        assert out["staged"] == {}


BOX = {"id": 14, "name": "Custom 11 x 8 x 4 in", "length": 10.98, "width": 7.99, "height": 4.02}
TUBE = {"id": 3, "name": "UPS Tube", "length": 0.0, "width": 0.0, "height": 0.0}


class TestStageBox:

    def test_box_resolves_read_only_then_dims_save_then_box_write(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)
        router.routes[(TMPL, "fb_resolve_box")] = lambda a, k: dict(BOX)
        out = ops.stage_listing(7, box="11x8x4")
        resolve = _by(mock_client, TMPL, "fb_resolve_box")
        assert resolve and resolve[0][2] == ["11x8x4"]
        vals = _saves(mock_client)[0]
        assert (vals["ebay_pkg_length_in"], vals["ebay_pkg_width_in"],
                vals["ebay_pkg_height_in"]) == (10.98, 7.99, 4.02)
        assert "ebay_package_type_id" not in vals      # not wizard-editable
        writes = [c for c in _by(mock_client, TMPL, "write")
                  if "ebay_package_type_id" in c[2][1]]
        # the saved dims ride along so sale_ebay's box autofill can't clobber them
        assert writes and writes[0][2] == [[7], {"ebay_package_type_id": 14, "ebay_pkg_length_in": 10.98, "ebay_pkg_width_in": 7.99, "ebay_pkg_height_in": 4.02}]
        assert _idx(mock_client, "ebay_wizard_save") < _idx(mock_client, "write",
                                                            ebay_package_type_id=14)
        assert out["staged"]["ebay_package_type_id"] == 14
        assert out["package"]["dims_source"] == "field"
        assert any("Box #14 Custom 11 x 8 x 4 in (10.98×7.99×4.02 in)" in n for n in out["notes"])

    def test_explicit_dims_beat_the_box_size(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)
        router.routes[(TMPL, "fb_resolve_box")] = lambda a, k: dict(BOX)
        ops.stage_listing(7, box=14, dims="18x12x6")
        vals = _saves(mock_client)[0]
        assert vals["ebay_pkg_length_in"] == 18.0
        assert _by(mock_client, TMPL, "fb_resolve_box")[0][2] == [14]
        box_write = [c for c in _by(mock_client, TMPL, "write")
                     if "ebay_package_type_id" in c[2][1]][0][2][1]
        assert box_write == {"ebay_package_type_id": 14, "ebay_pkg_length_in": 18.0,
                             "ebay_pkg_width_in": 12.0, "ebay_pkg_height_in": 6.0}

    def test_vals_height_beats_the_box_size_in_stage(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)
        router.routes[(TMPL, "fb_resolve_box")] = lambda a, k: dict(BOX)
        ops.stage_listing(7, vals={"ebay_pkg_height_in": 9}, box=14)
        vals = _saves(mock_client)[0]
        assert (vals["ebay_pkg_length_in"], vals["ebay_pkg_height_in"]) == (10.98, 9.0)
        box_write = [c for c in _by(mock_client, TMPL, "write")
                     if "ebay_package_type_id" in c[2][1]][0][2][1]
        assert box_write["ebay_pkg_height_in"] == 9.0

    def test_sizeless_box_is_written_without_dims(self, ops, mock_client):
        router = _stage_router(mock_client, PKG_TMPL)
        router.routes[(TMPL, "fb_resolve_box")] = lambda a, k: dict(TUBE)
        out = ops.stage_listing(7, box="UPS Tube")
        assert "ebay_pkg_length_in" not in _saves(mock_client)[0]
        assert out["staged"]["ebay_package_type_id"] == 3
        assert any("(no size)" in n for n in out["notes"])
        assert out["package"]["dims_source"] == "missing"

    @pytest.mark.parametrize("value", ["", "none", "clear", False, 0])
    def test_clearing_spellings_write_false_and_never_resolve(self, ops, mock_client, value):
        _stage_router(mock_client, PKG_TMPL)
        out = ops.stage_listing(7, box=value)
        assert not _by(mock_client, TMPL, "fb_resolve_box")
        writes = [c for c in _by(mock_client, TMPL, "write")
                  if "ebay_package_type_id" in c[2][1]]
        assert writes[0][2][1] == {"ebay_package_type_id": False}
        assert out["staged"]["ebay_package_type_id"] is False
        assert "Warehouse box cleared." in out["notes"]

    def test_no_box_arg_touches_nothing(self, ops, mock_client):
        _stage_router(mock_client, PKG_TMPL)
        out = ops.stage_listing(7)
        assert not _by(mock_client, TMPL, "fb_resolve_box")
        assert not [c for c in _by(mock_client, TMPL, "write")
                    if "ebay_package_type_id" in c[2][1]]
        assert "ebay_package_type_id" not in out["staged"]

    def test_server_box_error_propagates_before_any_write(self, ops, mock_client):
        from odoo_skill.errors import OdooError
        router = _stage_router(mock_client, PKG_TMPL)

        def boom(a, k):
            raise OdooError('2 boxes match "Box": #1 Box A, #2 Box B. Pick one by id.')
        router.routes[(TMPL, "fb_resolve_box")] = boom
        with pytest.raises(OdooError, match="Pick one by id"):
            ops.stage_listing(7, box="Box")
        assert not _saves(mock_client)

    def test_old_server_without_fb_module_gets_a_plain_error(self, ops, mock_client):
        from odoo_skill.errors import OdooError
        router = _stage_router(mock_client, PKG_TMPL)

        def missing(a, k):
            raise OdooError("Odoo error on product.template.fb_resolve_box: AttributeError: "
                            "The method 'fb_resolve_box' does not exist on the model 'product.template'")
        router.routes[(TMPL, "fb_resolve_box")] = missing
        with pytest.raises(OdooError, match="fb_marketplace_lister >= 4.4"):
            ops.stage_listing(7, box=14)
        # the method exists but raised → the server's own error surfaces
        router.routes[(TMPL, "fb_resolve_box")] = lambda a, k: (_ for _ in ()).throw(
            OdooError("Odoo error on product.template.fb_resolve_box: No box matches \"14\""))
        with pytest.raises(OdooError, match="No box matches"):
            ops.stage_listing(7, box=14)

    @pytest.mark.parametrize("value", [1.5, [1], {"id": 1}])
    def test_bad_box_value_refused_before_rpc(self, ops, mock_client, value):
        _stage_router(mock_client, PKG_TMPL)
        with pytest.raises(ValueError, match="box must be"):
            ops.stage_listing(7, box=value)
        assert not _calls(mock_client)


class TestRevisePackage:

    def test_box_dims_go_through_the_revise_stage_then_the_box_is_written(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "ebay_wizard_revise_stage"): {"success": True, "diff": [], "hash": "h",
                                                "warnings": ["Nothing changed."]},
            (TMPL, "fb_resolve_box"): lambda a, k: dict(BOX)})
        out = ops.revise_stage(7, box="Custom 11")
        vals = _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0][2][1]
        assert vals == {"ebay_pkg_length_in": 10.98, "ebay_pkg_width_in": 7.99,
                        "ebay_pkg_height_in": 4.02}
        assert _idx(mock_client, "ebay_wizard_revise_stage") < _idx(mock_client, "write",
                                                                    ebay_package_type_id=14)
        assert _by(mock_client, TMPL, "write")[0][2][1] == {"ebay_package_type_id": 14, **vals}
        assert out["box_id"] == 14
        assert out["warnings"][-1].startswith("Box #14 ")

    def test_explicit_vals_beat_the_box_size_on_revise(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "ebay_wizard_revise_stage"): {"success": True, "diff": [], "hash": "h"},
            (TMPL, "fb_resolve_box"): lambda a, k: dict(BOX)})
        ops.revise_stage(7, vals={"ebay_pkg_height_in": 9}, box=14)
        vals = _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0][2][1]
        assert vals == {"ebay_pkg_length_in": 10.98, "ebay_pkg_width_in": 7.99,
                        "ebay_pkg_height_in": 9}
        assert _by(mock_client, TMPL, "write")[0][2][1]["ebay_pkg_height_in"] == 9

    def test_refused_revise_stage_does_not_write_the_box(self, ops, mock_client):
        Router(mock_client, {
            (TMPL, "ebay_wizard_revise_stage"): {"success": False, "reason": "not_live",
                                                "message": "not live"},
            (TMPL, "fb_resolve_box"): lambda a, k: dict(BOX)})
        out = ops.revise_stage(7, box=14)
        assert not _by(mock_client, TMPL, "write")
        assert "box_id" not in out

    def test_clearing_the_box_on_revise_keeps_the_dims(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {"success": True, "diff": [], "hash": "h"}})
        out = ops.revise_stage(7, box="")
        assert _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0][2][1] == {}
        assert _by(mock_client, TMPL, "write")[0][2] == [[7], {"ebay_package_type_id": False}]
        assert out["box_id"] is False


    def test_weight_and_dims_become_vals(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_wizard_revise_stage"): {"success": True, "diff": [], "hash": "h"}})
        ops.revise_stage(7, {"ebay_fixed_price": 99.0}, weight_lb="2.5", dims="18 x 12 x 6 in")
        vals = _by(mock_client, TMPL, "ebay_wizard_revise_stage")[0][2][1]
        assert vals == {"ebay_fixed_price": 99.0, "weight": 2.5, "ebay_pkg_length_in": 18.0,
                        "ebay_pkg_width_in": 12.0, "ebay_pkg_height_in": 6.0}

    def test_bad_weight_refused_before_rpc(self, ops, mock_client):
        Router(mock_client, {})
        with pytest.raises(ValueError, match="weight_lb"):
            ops.revise_stage(7, weight_lb=-1)
        assert not _calls(mock_client)
