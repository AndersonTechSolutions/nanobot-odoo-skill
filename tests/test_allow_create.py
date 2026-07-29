"""Tests for the opt-in auto-creation contract introduced in v3.0.0.

Before v3, a fuzzy lookup miss silently created the record — "quote for Rocky
with product Rock" minted a partner *and* a $0 consumable in the live
catalogue. Creation is now gated behind ``allow_create``.

These tests pin the default (blocking) path. The tests in
test_smart_actions.py cover the opt-in path by passing ``allow_create=True``.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.smart_actions import (  # noqa: E402
    MissingDependency,
    SmartActionHandler,
    _needs_confirmation,
)


# ── Resolver-level gating ────────────────────────────────────────────


class TestResolverGating:
    """find_or_create_* must not write when allow_create is off."""

    def test_partner_miss_raises_instead_of_creating(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [
            [],   # strict search_read finds nothing
            [],   # near-match lookup also empty
        ]
        with pytest.raises(MissingDependency) as exc:
            smart.find_or_create_partner("NewCo")
        assert exc.value.kind == "customer"
        assert exc.value.query == "NewCo"

    def test_product_miss_raises_instead_of_creating(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [[], []]
        with pytest.raises(MissingDependency) as exc:
            smart.find_or_create_product("Widget")
        assert exc.value.kind == "product"

    def test_no_create_rpc_is_issued_on_miss(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [[], []]
        with pytest.raises(MissingDependency):
            smart.find_or_create_partner("Ghost")
        methods = [
            call[0][4] for call in mock_client._models.execute_kw.call_args_list
        ]
        assert "create" not in methods, f"a create slipped through: {methods}"

    def test_existing_record_still_resolves(self, smart, mock_client):
        """Gating must not disturb the hit path."""
        mock_client._models.execute_kw.return_value = [
            {"id": 10, "name": "Acme Corp", "email": "", "phone": "",
             "is_company": True, "customer_rank": 1, "supplier_rank": 0},
        ]
        result = smart.find_or_create_partner("Acme Corp")
        assert result["created"] is False
        assert result["partner"]["id"] == 10


# ── Handler-level policy ─────────────────────────────────────────────


class TestAllowCreatePolicy:

    def test_default_is_off(self, mock_client):
        assert SmartActionHandler(mock_client).allow_create is False

    def test_constructor_opt_in(self, mock_client):
        assert SmartActionHandler(mock_client, allow_create=True).allow_create is True

    def test_per_call_overrides_handler_default(self, mock_client):
        h = SmartActionHandler(mock_client, allow_create=True)
        assert h._may_create(None) is True      # falls back to handler default
        assert h._may_create(False) is False    # per-call wins
        h2 = SmartActionHandler(mock_client)
        assert h2._may_create(None) is False
        assert h2._may_create(True) is True


# ── needs_confirmation envelope ──────────────────────────────────────


class TestNeedsConfirmation:

    def test_envelope_reports_nothing_created(self):
        out = _needs_confirmation(
            MissingDependency("product", "Rock",
                              [{"id": 812, "name": "Rock Tumbler"}])
        )
        assert out["status"] == "needs_confirmation"
        assert out["created_anything"] is False
        assert out["missing"][0]["kind"] == "product"
        assert "Rock Tumbler" in out["summary"]
        assert "allow_create=True" in out["summary"]

    def test_envelope_without_near_matches(self):
        out = _needs_confirmation(MissingDependency("customer", "Zzqx", []))
        assert out["created_anything"] is False
        assert "No near matches" in out["summary"]


# ── Composite actions return, not raise ──────────────────────────────


class TestGatedCompositeActions:
    """smart_create_* converts the miss into a result a chat agent can use."""

    def test_quotation_returns_needs_confirmation(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [[], []]
        result = smart.smart_create_quotation(
            customer_name="Ghost Co",
            product_lines=[{"name": "Nothing"}],
        )
        assert result["status"] == "needs_confirmation"
        assert result["created_anything"] is False
        assert result["missing"][0]["kind"] == "customer"

    def test_purchase_returns_needs_confirmation(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [[], []]
        result = smart.smart_create_purchase(
            vendor_name="Ghost Vendor",
            product_lines=[{"name": "Nothing"}],
        )
        assert result["status"] == "needs_confirmation"
        assert result["created_anything"] is False
