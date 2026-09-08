"""Stale-listing digest wrappers on ``EbayListingOps`` (sale_ebay >= 1.47.0).

The server owns every rule; these tests pin the connector's contract:

* every write is a dry run unless ``confirm=True`` and never calls eBay
  through any method but the allowlisted stale actions;
* ``set_sold_comps`` stamps the sold check only for sold-browser data and
  only when the server has the digest;
* review rows get digest text and the summary counts.
"""

import json
import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.errors import OdooError  # noqa: E402
from odoo_skill.models.ebay_listing import EbayListingOps  # noqa: E402
from tests.test_ebay_listing import Router, _by  # noqa: E402
from tests.test_new_connectors import _ready  # noqa: E402

TMPL = "product.template"
STALE_FIELDS = {"ebay_use", "ebay_stale_verdict", "ebay_sold_checked_at", "ebay_age_bucket"}


@pytest.fixture()
def ops(mock_client):
    o = _ready(EbayListingOps, mock_client)
    o._model_field_cache = set(STALE_FIELDS)
    return o


@pytest.fixture()
def old_ops(mock_client):
    """Server without the digest (sale_ebay < 1.47.0)."""
    o = _ready(EbayListingOps, mock_client)
    o._model_field_cache = {"ebay_use"}
    return o


def _rec(**over):
    base = {"id": 7, "name": "Dell OptiPlex 7090", "ebay_fixed_price": 100.0,
            "ebay_suggested_price": 85.0, "ebay_suggested_discount_pct": 15.0,
            "ebay_comp_median": 88.0, "ebay_comp_count": 6, "ebay_days_listed": 75,
            "ebay_age_bucket": "d60", "ebay_stale_verdict": "cut",
            "ebay_stale_verdict_note": "Cut 100.00 → 85.00 (−15.0%).",
            "ebay_stale_promo_pct": 0, "ebay_listing_status": "Active"}
    base.update(over)
    return base


REVIEW = {
    "counts": {"live": 307, "unsold": 272, "by_bucket": {"d30": 1, "d60": 1, "d90": 1, "older": 0},
               "by_verdict": {"promo": 1, "cut": 1, "end": 1}, "needs_research": 2, "recheck_days": 30},
    "buckets": {
        "d30": [{"id": 1, "name": "A", "price": 249.0, "comp_median": 229.0, "comp_count": 9,
                 "verdict": "promo", "promo_pct": 10, "note": "10% markdown"}],
        "d60": [{"id": 2, "name": "B", "price": 399.0, "suggested": 349.0, "discount_pct": 12.5,
                 "comp_median": 359.0, "comp_count": 12, "verdict": "cut", "note": "Cut"}],
        "d90": [{"id": 3, "name": "C", "price": 89.0, "qty_on_hand": 1.0, "comp_count": 0,
                 "verdict": "end", "note": "No sold comps after 120 days."}],
        "older": [],
    },
    "needs_research": [9, 8],
}


class TestGate:

    def test_requires_server_digest(self, old_ops, mock_client):
        Router(mock_client)
        with pytest.raises(OdooError, match="1.47.0"):
            old_ops.stale_review()
        with pytest.raises(OdooError):
            old_ops.apply_stale_cut(7, confirm=True)
        assert not _by(mock_client, TMPL, "action_ebay_stale_cut")

    def test_stale_actions_allowlisted(self, ops):
        assert "action_ebay_stale_cut" in ops.ALLOWED_ACTIONS
        assert "action_ebay_stale_end_and_scrap" in ops.ALLOWED_ACTIONS


class TestReview:

    def test_review_adds_text_and_summary(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_stale_review"): REVIEW})
        out = ops.stale_review(limit=5, buckets=["d30", "d60"])
        call = _by(mock_client, TMPL, "ebay_stale_review")[0]
        assert call[2] == [] and call[3] == {"limit": 5, "buckets": ["d30", "d60"]}
        assert out["buckets"]["d30"][0]["text"] == "#1 A $249 · med $229 (n=9) → 10% markdown"
        assert out["buckets"]["d60"][0]["text"] == "#2 B $399 → $349 (−12.5%, med $359 n=12)"
        assert out["buckets"]["d90"][0]["text"] == "#3 C $89 · no sold comps · on hand 1"
        assert out["summary"] == ("Live 307 · unsold 272 · needs research 2 · "
                                  "promo 1 · cut 1 · end 1 · hold 0")

    def test_needs_research_keeps_server_order_and_limits(self, ops, mock_client):
        Router(mock_client, {(TMPL, "ebay_stale_review"): REVIEW})
        assert ops.stale_needs_research() == [9, 8]
        assert ops.stale_needs_research(limit=1) == [9]


class TestSoldStamp:

    PRICES = [80, 60, 100, 70, 90]

    def test_set_sold_comps_stamps_after_write(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec()]})
        out = ops.set_sold_comps(7, self.PRICES)
        methods = [a.args[4] for a in mock_client._models.execute_kw.call_args_list]
        assert methods.index("write") < methods.index("ebay_stale_mark_sold_checked")
        assert _by(mock_client, TMPL, "ebay_stale_mark_sold_checked")[0][2] == [[7]]
        assert json.loads(_by(mock_client, TMPL, "write")[0][2][1]["ebay_comp_json"])["source"] == "ebay_sold_browser"
        assert out["stamped"] is True

    def test_set_sold_comps_never_stamps_other_sources(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec()]})
        out = ops.set_sold_comps(7, self.PRICES, source="ebay_browse")
        assert not _by(mock_client, TMPL, "ebay_stale_mark_sold_checked")
        assert out["stamped"] is False

    def test_set_sold_comps_stamp_opt_out(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec()]})
        ops.set_sold_comps(7, self.PRICES, stamp=False)
        assert not _by(mock_client, TMPL, "ebay_stale_mark_sold_checked")

    def test_set_sold_comps_on_old_server_skips_stamp(self, old_ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec()]})
        out = old_ops.set_sold_comps(7, self.PRICES)
        assert out["written"] is True and out["stamped"] is False
        assert not _by(mock_client, TMPL, "ebay_stale_mark_sold_checked")

    def test_record_no_comps(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec(ebay_stale_verdict="end",
                                                   ebay_stale_verdict_note="No sold comps after 120 days.")]})
        out = ops.record_no_comps(7)
        assert _by(mock_client, TMPL, "ebay_stale_record_no_comps")[0][2] == [[7]]
        assert out["verdict"] == "end" and "No sold comps" in out["summary"]

    def test_mark_sold_checked(self, ops, mock_client):
        Router(mock_client)
        assert ops.mark_sold_checked(7)["stamped"] is True
        assert _by(mock_client, TMPL, "ebay_stale_mark_sold_checked")[0][2] == [[7]]


class TestCut:

    def test_dry_run_by_default(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec()]})
        out = ops.apply_stale_cut(7)
        assert out["applied"] is False and "Dry run" in out["summary"]
        assert "100.00 → 85.00" in out["summary"]
        assert not _by(mock_client, TMPL, "action_ebay_stale_cut")

    def test_refuses_non_cut_verdict_client_side(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec(ebay_stale_verdict="promo")]})
        out = ops.apply_stale_cut(7, confirm=True)
        assert out["applied"] is False and "not a cut candidate" in out["summary"]
        assert not _by(mock_client, TMPL, "action_ebay_stale_cut")

    def test_refuses_over_ceiling_unless_raised(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec(ebay_suggested_discount_pct=30.0)],
                             (TMPL, "action_ebay_stale_cut"): {
                                 "product_id": 7, "old_price": 100.0, "new_price": 70.0,
                                 "discount_pct": 30.0, "status": "Active"}})
        out = ops.apply_stale_cut(7, confirm=True)
        assert out["applied"] is False and "exceeds the 25.0% ceiling" in out["summary"]
        assert not _by(mock_client, TMPL, "action_ebay_stale_cut")
        out = ops.apply_stale_cut(7, confirm=True, max_discount_pct=40)
        call = _by(mock_client, TMPL, "action_ebay_stale_cut")[0]
        assert call[2] == [[7]] and call[3] == {"max_discount_pct": 40.0}
        assert out["applied"] is True and "100.00 → 70.00" in out["summary"]

    def test_confirm_uses_stale_action_not_local_write(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec()],
                             (TMPL, "action_ebay_stale_cut"): {
                                 "product_id": 7, "old_price": 100.0, "new_price": 85.0,
                                 "discount_pct": 15.0, "status": "Active"}})
        out = ops.apply_stale_cut(7, confirm=True)
        assert out["applied"] is True and "100.00 → 85.00 (−15.0%)" in out["summary"]
        calls = _by(mock_client, TMPL, "action_ebay_stale_cut")
        assert len(calls) == 1 and calls[0][2] == [[7]] and calls[0][3] == {"max_discount_pct": 25.0}
        assert not _by(mock_client, TMPL, "write")


class TestEndScrap:

    PREVIEW = {"product_id": 7, "ebay_id": "1680001", "listing_status": "Active",
               "scrap": [{"location": "WH/Stock", "lot": "SN-A", "qty": 1.0},
                         {"location": "WH/Stock", "lot": "", "qty": 2.0}], "total_qty": 3.0}

    def test_preview_summary(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec(ebay_stale_verdict="end")],
                             (TMPL, "ebay_stale_end_preview"): self.PREVIEW})
        out = ops.stale_end_preview(7)
        assert out["summary"] == ("Dell OptiPlex 7090: listing Active, would scrap 3 unit(s): "
                                  "1@WH/Stock lot SN-A, 2@WH/Stock")

    def test_dry_run_and_verdict_guard(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec(ebay_stale_verdict="end")],
                             (TMPL, "ebay_stale_end_preview"): self.PREVIEW})
        out = ops.end_stale_scrap(7)
        assert out["ended"] is False and "Dry run" in out["summary"]
        assert not _by(mock_client, TMPL, "action_ebay_stale_end_and_scrap")
        Router(mock_client, {(TMPL, "read"): [_rec(ebay_stale_verdict="cut")],
                             (TMPL, "ebay_stale_end_preview"): self.PREVIEW})
        out = ops.end_stale_scrap(7, confirm=True)
        assert out["ended"] is False and "not an end candidate" in out["summary"]
        assert not _by(mock_client, TMPL, "action_ebay_stale_end_and_scrap")

    def test_confirm_runs_server_action(self, ops, mock_client):
        Router(mock_client, {(TMPL, "read"): [_rec(ebay_stale_verdict="end")],
                             (TMPL, "ebay_stale_end_preview"): self.PREVIEW,
                             (TMPL, "action_ebay_stale_end_and_scrap"): {
                                 "product_id": 7, "ended": True, "archived": True, "status": "Ended",
                                 "scrapped": [{"location": "WH/Stock", "lot": "SN-A", "qty": 1.0},
                                              {"location": "WH/Stock", "lot": "", "qty": 2.0}]}})
        out = ops.end_stale_scrap(7, confirm=True)
        assert _by(mock_client, TMPL, "action_ebay_stale_end_and_scrap")[0][2] == [[7]]
        assert out["ended"] is True
        assert out["summary"] == "Dell OptiPlex 7090: listing ended, scrapped 3 unit(s), product archived."
        assert not _by(mock_client, TMPL, "action_end_single_listing")


class TestPromo:

    def test_drafts_batch_and_reports_existing(self, ops, mock_client):
        Router(mock_client, {(TMPL, "action_ebay_stale_promo"): {
            "promotion_id": 55, "name": "Stale 30d 10% (2026-09-15)", "pct": 10,
            "product_ids": [1, 2], "existing": False}})
        out = ops.create_stale_promo([1, 2])
        call = _by(mock_client, TMPL, "action_ebay_stale_promo")[0]
        assert call[2] == [[1, 2]] and call[3] == {}
        assert out["summary"].startswith("Drafted promo #55")
        assert "approve promo 55" in out["summary"]
        Router(mock_client, {(TMPL, "action_ebay_stale_promo"): {
            "promotion_id": 55, "name": "n", "pct": 15, "product_ids": [1], "existing": True}})
        out = ops.create_stale_promo([1], pct=15)
        assert _by(mock_client, TMPL, "action_ebay_stale_promo")[-1][3] == {"pct": 15}
        assert out["summary"].startswith("Existing promo #55")

    def test_refuses_empty(self, ops, mock_client):
        Router(mock_client)
        with pytest.raises(OdooError):
            ops.create_stale_promo([])
