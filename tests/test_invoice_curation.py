"""Tests for invoice curation + QuickBooks reconciliation ops.

These lock the decisions a live smoke test cannot catch:

* Curation must go reset → edit → post. ``update_invoice_lines`` **refuses**
  to touch a non-draft invoice, so a posted invoice is never silently edited
  behind the connector's back.
* The ``invoice_line_ids`` command vector must be exactly the (1,…)/(0,…)/(2,…)
  triples Odoo expects — a malformed command does not error, it writes the
  wrong thing.
* Reconciliation is driven through the module's own hooks (button methods and
  the self-heal cron), never by writing ``state`` or reimplementing the sweep.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.models.invoice import InvoiceOps  # noqa: E402


def _calls(mock_client):
    """Every execute_kw call as (model, method, args_vector, odoo_kwargs)."""
    out = []
    for call in mock_client._models.execute_kw.call_args_list:
        args = call[0]
        odoo_kwargs = args[6] if len(args) > 6 else {}
        out.append((args[3], args[4], args[5], odoo_kwargs))
    return out


@pytest.fixture()
def invoices(mock_client):
    return InvoiceOps(mock_client)


class TestCurationGuards:

    def test_update_refuses_a_posted_invoice(self, invoices, mock_client):
        """Editing lines on a posted invoice must raise, not write."""
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "name": "INV/2026/0001", "state": "posted"}
        ]
        with pytest.raises(ValueError, match="not draft"):
            invoices.update_invoice_lines(
                42, line_updates=[{"line_id": 118, "price_unit": 600.0}])
        # Only the state read happened — no write reached the wire.
        assert not any(c[1] == "write" for c in _calls(mock_client))

    def test_update_refuses_when_nothing_changes(self, invoices, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "name": "INV/2026/0001", "state": "draft",
             "invoice_line_ids": []}
        ]
        with pytest.raises(ValueError, match="No line changes"):
            invoices.update_invoice_lines(42)

    def test_update_refuses_a_line_from_another_invoice(
        self, invoices, mock_client
    ):
        """A line_id not on this invoice must be refused, not unlinked."""
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "name": "INV/2026/0001", "state": "draft",
             "invoice_line_ids": [118]}
        ]
        with pytest.raises(ValueError, match="not on invoice"):
            invoices.update_invoice_lines(42, remove_line_ids=[999])
        assert not any(c[1] == "write" for c in _calls(mock_client))


class TestCommandVector:

    def test_line_commands_are_wellformed(self, invoices, mock_client):
        """Update/add/remove map to (1,…),(0,0,…),(2,…) respectively."""
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "name": "INV/2026/0001", "state": "draft",
             "invoice_line_ids": [118, 119]}
        ]
        invoices.update_invoice_lines(
            42,
            line_updates=[{"line_id": 118, "price_unit": 600.0, "quantity": 2}],
            add_lines=[{"price_unit": 25.0, "description": "Rush fee",
                        "product_id": 7}],
            remove_line_ids=[119],
        )
        writes = [c for c in _calls(mock_client) if c[1] == "write"]
        assert len(writes) == 1
        _, _, args, _ = writes[0]
        commands = args[1]["invoice_line_ids"]
        assert (1, 118, {"price_unit": 600.0, "quantity": 2}) in commands
        add = next(c for c in commands if c[0] == 0)
        assert add[2]["price_unit"] == 25.0 and add[2]["product_id"] == 7
        assert (2, 119) in commands

    def test_tax_ids_use_a_replace_command(self, invoices, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "name": "INV/1", "state": "draft",
             "invoice_line_ids": [5]}
        ]
        invoices.update_invoice_lines(
            42, line_updates=[{"line_id": 5, "tax_ids": [3]}])
        writes = [c for c in _calls(mock_client) if c[1] == "write"]
        cmd = writes[0][2][1]["invoice_line_ids"][0]
        assert cmd == (1, 5, {"tax_ids": [(6, 0, [3])]})


class TestModuleHooks:

    def test_reset_to_draft_uses_button(self, invoices, mock_client):
        mock_client._models.execute_kw.return_value = [{"id": 42}]
        invoices.reset_to_draft(42)
        assert ("account.move", "button_draft", [[42]], {}) in _calls(mock_client)

    def test_void_cancels_first_then_records_reason(self, invoices, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "state": "cancel", "narration": "orig"}
        ]
        invoices.void_invoice(42, reason="duplicate")
        calls = _calls(mock_client)
        # cancel must fire before the narration write — no pre-mutation
        cancel_idx = next(i for i, c in enumerate(calls) if c[1] == "button_cancel")
        write_idx = next(i for i, c in enumerate(calls) if c[1] == "write")
        assert cancel_idx < write_idx
        write = calls[write_idx]
        assert "Voided: duplicate" in write[2][1]["narration"]

    def test_void_raises_if_cancel_did_not_take(self, invoices, mock_client):
        """If the move is still posted after button_cancel, do not record."""
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "name": "INV/1", "state": "posted"}
        ]
        with pytest.raises(ValueError, match="did not cancel"):
            invoices.void_invoice(42, reason="duplicate")
        assert not any(c[1] == "write" for c in _calls(mock_client))

    def test_void_without_reason_skips_the_write(self, invoices, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 42, "state": "cancel"}
        ]
        invoices.void_invoice(42)
        assert not any(c[1] == "write" for c in _calls(mock_client))
        assert any(c[1] == "button_cancel" for c in _calls(mock_client))

    def test_push_qbo_update_calls_module_button(self, invoices, mock_client):
        mock_client._models.execute_kw.return_value = [{"id": 42}]
        invoices.push_qbo_update(42)
        assert any(c[1] == "action_qbo_push_update" for c in _calls(mock_client))

    def test_selfheal_triggers_the_cron_not_a_private_method(
        self, invoices, mock_client
    ):
        """The sweep runs via ir.cron.method_direct_trigger.

        Its real body is ``_cron_qbo_payment_selfheal`` — an underscore-
        prefixed method the XML-RPC layer refuses to call. Resolving the cron
        record and triggering it is the only path that works over the API.
        """
        mock_client._models.execute_kw.return_value = ("ir.cron", 7)
        out = invoices.run_qbo_selfheal()
        calls = _calls(mock_client)
        assert ("ir.model.data", "check_object_reference",
                ["atech_qbo_invoice_autolink", "cron_qbo_payment_selfheal"],
                {}) in calls
        assert ("ir.cron", "method_direct_trigger", [[7]], {}) in calls
        assert out == {"triggered": True, "cron_id": 7}


class TestReconciliationStatus:

    def test_unmapped_invoice_reports_not_mapped(self, invoices, mock_client):
        # get_reconciliation_status: first read = invoice, then mapping search
        mock_client._models.execute_kw.side_effect = [
            [{"name": "INV/1", "state": "posted", "payment_state": "not_paid",
              "amount_residual": 100.0, "amount_total": 100.0}],
            [],  # no qbo.map.account.move row
        ]
        out = invoices.get_reconciliation_status(42)
        assert out["qbo"] == {"mapped": False}
        assert out["invoice"]["name"] == "INV/1"

    def test_mapped_invoice_surfaces_selfheal_state(self, invoices, mock_client):
        mock_client._models.execute_kw.side_effect = [
            [{"name": "INV/1", "state": "posted", "payment_state": "not_paid",
              "amount_residual": 53.5, "amount_total": 53.5}],
            [{"qbo_id": "130", "atech_selfheal_attempts": 3,
              "atech_selfheal_last_attempt": "2026-08-13 03:22:15",
              "atech_selfheal_escalated": True}],
        ]
        out = invoices.get_reconciliation_status(42)
        assert out["qbo"]["mapped"] is True
        assert out["qbo"]["selfheal_attempts"] == 3
        assert out["qbo"]["selfheal_escalated"] is True
