"""
Invoice operations for Odoo ``account.move``.
"""

import logging
from datetime import date
from typing import Any, Optional

from ..client import OdooClient

logger = logging.getLogger("odoo_skill")

_INVOICE_LIST_FIELDS = [
    "id", "name", "partner_id", "move_type", "state",
    "payment_state", "invoice_date", "invoice_date_due",
    "amount_untaxed", "amount_total", "amount_residual",
    "currency_id",
]

_INVOICE_DETAIL_FIELDS = _INVOICE_LIST_FIELDS + [
    "invoice_line_ids", "ref", "narration",
    "company_id", "user_id",
]


class InvoiceOps:
    """High-level operations for Odoo invoices (``account.move``).

    Args:
        client: An authenticated :class:`OdooClient` instance.
    """

    MODEL = "account.move"

    def __init__(self, client: OdooClient) -> None:
        self.client = client

    # ── Create ───────────────────────────────────────────────────────

    def create_invoice(
        self,
        partner_id: int,
        lines: list[dict],
        invoice_date: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Create a customer invoice.

        Args:
            partner_id: Customer ID.
            lines: List of line dicts, each containing at minimum
                ``price_unit`` and optionally ``product_id``, ``quantity``,
                ``description``, ``account_id``.
            invoice_date: Invoice date as ``YYYY-MM-DD`` string.
            **extra: Additional ``account.move`` field values.

        Returns:
            The newly created invoice record.
        """
        invoice_lines = []
        for line in lines:
            il: dict[str, Any] = {
                "name": line.get("description", line.get("name", "")),
                "quantity": line.get("quantity", 1),
                "price_unit": line["price_unit"],
            }
            if "product_id" in line:
                il["product_id"] = line["product_id"]
            if "account_id" in line:
                il["account_id"] = line["account_id"]
            if "tax_ids" in line:
                il["tax_ids"] = [(6, 0, line["tax_ids"])]
            invoice_lines.append((0, 0, il))

        values: dict[str, Any] = {
            "move_type": "out_invoice",
            "partner_id": partner_id,
            "invoice_line_ids": invoice_lines,
        }
        if invoice_date:
            values["invoice_date"] = invoice_date
        values.update(extra)

        invoice_id = self.client.create(self.MODEL, values)
        logger.info("Created invoice for partner_id=%d → id=%d", partner_id, invoice_id)
        return self.get_invoice(invoice_id)

    # ── Workflow ─────────────────────────────────────────────────────

    def post_invoice(self, invoice_id: int) -> dict:
        """Post (validate) a draft invoice.

        Args:
            invoice_id: The invoice (``account.move``) ID.

        Returns:
            The updated invoice record.
        """
        self.client.execute(self.MODEL, "action_post", [invoice_id])
        logger.info("Posted invoice id=%d", invoice_id)
        return self.get_invoice(invoice_id)

    # ── Read ─────────────────────────────────────────────────────────

    def get_invoice(self, invoice_id: int) -> dict:
        """Get full details of a single invoice.

        Args:
            invoice_id: The invoice ID.

        Returns:
            Invoice record dict, or empty dict if not found.
        """
        records = self.client.read(self.MODEL, invoice_id, fields=_INVOICE_DETAIL_FIELDS)
        return records[0] if records else {}

    def get_unpaid_invoices(
        self,
        partner_id: Optional[int] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Get customer invoices that are not fully paid.

        Args:
            partner_id: Optionally filter by customer.
            limit: Max results.

        Returns:
            List of unpaid invoice records, ordered by due date.
        """
        domain: list = [
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["payment_state", "in", ["not_paid", "partial"]],
        ]
        if partner_id:
            domain.append(["partner_id", "=", partner_id])

        return self.client.search_read(
            self.MODEL, domain, fields=_INVOICE_LIST_FIELDS,
            limit=limit, order="invoice_date_due asc",
        )

    def get_overdue_invoices(self, limit: int = 50) -> list[dict]:
        """Get all overdue customer invoices.

        Returns invoices that are posted, not fully paid, and past
        their due date.

        Args:
            limit: Max results.

        Returns:
            List of overdue invoice records, oldest first.
        """
        today = date.today().isoformat()
        domain: list = [
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["payment_state", "in", ["not_paid", "partial"]],
            ["invoice_date_due", "<", today],
        ]
        return self.client.search_read(
            self.MODEL, domain, fields=_INVOICE_LIST_FIELDS,
            limit=limit, order="invoice_date_due asc",
        )

    def get_invoice_lines(self, invoice_id: int) -> list[dict]:
        """Read the editable line detail of an invoice.

        Args:
            invoice_id: The invoice (``account.move``) ID.

        Returns:
            List of line dicts (``account.move.line``) with the fields a
            bookkeeper edits: label, product, quantity, unit price, taxes,
            and computed subtotal. Section/note lines are included so line
            IDs line up with what the user sees in Odoo.
        """
        inv = self.client.read(self.MODEL, invoice_id, fields=["invoice_line_ids"])
        if not inv:
            return []
        line_ids = inv[0].get("invoice_line_ids") or []
        if not line_ids:
            return []
        return self.client.read(
            "account.move.line", line_ids,
            fields=[
                "id", "display_type", "name", "product_id", "quantity",
                "price_unit", "discount", "tax_ids", "price_subtotal",
                "price_total",
            ],
        )

    # ── Curation (edit a posted invoice safely) ──────────────────────

    def reset_to_draft(self, invoice_id: int) -> dict:
        """Reset a posted invoice back to draft so its lines can be edited.

        Uses the native ``button_draft``. Re-posting afterwards is what
        triggers the QuickBooks update push (the connector's ``_post``
        override), so the correct curation flow is:
        ``reset_to_draft`` → ``update_invoice_lines`` → ``post_invoice``.

        Args:
            invoice_id: The invoice (``account.move``) ID.

        Returns:
            The updated invoice record.
        """
        self.client.execute(self.MODEL, "button_draft", [invoice_id])
        logger.info("Reset invoice id=%d to draft", invoice_id)
        return self.get_invoice(invoice_id)

    def update_invoice_lines(
        self,
        invoice_id: int,
        line_updates: Optional[list[dict]] = None,
        add_lines: Optional[list[dict]] = None,
        remove_line_ids: Optional[list[int]] = None,
    ) -> dict:
        """Edit the lines of a **draft** invoice.

        The invoice must already be in draft — call :meth:`reset_to_draft`
        first for a posted one. This never resets or posts on its own; the
        caller owns those steps so the confirmation and the QBO push stay
        explicit.

        Args:
            invoice_id: The invoice (``account.move``) ID.
            line_updates: Existing lines to change. Each dict needs
                ``line_id`` plus any of ``price_unit``, ``quantity``,
                ``name`` (label), ``discount``.
            add_lines: New lines, same shape as ``create_invoice`` lines
                (``price_unit`` required; optional ``product_id``,
                ``quantity``, ``description``/``name``, ``account_id``,
                ``tax_ids``).
            remove_line_ids: Line IDs to delete.

        Returns:
            The updated invoice record.

        Raises:
            ValueError: If the invoice is not in draft, or nothing to do.
        """
        current = self.get_invoice(invoice_id)
        if current.get("state") != "draft":
            raise ValueError(
                f"Invoice {current.get('name') or invoice_id} is "
                f"{current.get('state')!r}, not draft. Call reset_to_draft "
                f"first."
            )

        # Guard against editing a line that belongs to another invoice (or a
        # stale ID): every referenced line must be a member of this invoice.
        # (2, id) unlinks unconditionally, so a wrong ID would silently delete
        # someone else's line.
        member_ids = set(current.get("invoice_line_ids") or [])
        referenced = {u["line_id"] for u in (line_updates or [])} | \
            set(remove_line_ids or [])
        stray = referenced - member_ids
        if stray:
            raise ValueError(
                f"Line IDs {sorted(stray)} are not on invoice "
                f"{current.get('name') or invoice_id}. Refusing to edit lines "
                f"that belong to another record."
            )

        commands: list = []
        for upd in line_updates or []:
            line_id = upd["line_id"]
            vals = {
                k: upd[k]
                for k in ("price_unit", "quantity", "name", "discount")
                if k in upd
            }
            if "tax_ids" in upd:
                vals["tax_ids"] = [(6, 0, upd["tax_ids"])]
            if vals:
                commands.append((1, line_id, vals))
        for add in add_lines or []:
            il: dict[str, Any] = {
                "name": add.get("description", add.get("name", "")),
                "quantity": add.get("quantity", 1),
                "price_unit": add["price_unit"],
            }
            for opt in ("product_id", "account_id", "discount"):
                if opt in add:
                    il[opt] = add[opt]
            if "tax_ids" in add:
                il["tax_ids"] = [(6, 0, add["tax_ids"])]
            commands.append((0, 0, il))
        for line_id in remove_line_ids or []:
            commands.append((2, line_id))

        if not commands:
            raise ValueError("No line changes supplied.")

        self.client.write(self.MODEL, invoice_id, {"invoice_line_ids": commands})
        logger.info(
            "Edited %d line command(s) on invoice id=%d", len(commands), invoice_id)
        return self.get_invoice(invoice_id)

    def reprice_line(
        self, invoice_id: int, line_id: int, price_unit: float,
    ) -> dict:
        """Change the unit price of one draft-invoice line.

        Convenience wrapper over :meth:`update_invoice_lines` for the common
        "change the price" curation.

        Args:
            invoice_id: The invoice (``account.move``) ID.
            line_id: The ``account.move.line`` ID to reprice.
            price_unit: New unit price.

        Returns:
            The updated invoice record.
        """
        return self.update_invoice_lines(
            invoice_id, line_updates=[{"line_id": line_id, "price_unit": price_unit}],
        )

    def void_invoice(
        self, invoice_id: int, reason: Optional[str] = None,
    ) -> dict:
        """Cancel (void) an invoice, recording the reason in the narration.

        Uses the native ``button_cancel``. The reason is written to the
        invoice ``narration`` so the audit trail survives — Odoo has no
        dedicated void-reason field, mirroring the QBO skill's PrivateNote
        convention.

        Args:
            invoice_id: The invoice (``account.move``) ID.
            reason: Why the invoice is being voided.

        Returns:
            The updated invoice record.
        """
        # Cancel first, then record the reason — never mutate the invoice
        # unless the cancel actually took. A posted move can refuse to cancel
        # (journal lock date, hash chain), so verify the post-condition rather
        # than assuming "void" succeeded.
        self.client.execute(self.MODEL, "button_cancel", [invoice_id])
        updated = self.get_invoice(invoice_id)
        if updated.get("state") != "cancel":
            raise ValueError(
                f"Invoice {updated.get('name') or invoice_id} did not cancel "
                f"(state={updated.get('state')!r}); its journal lock date or "
                f"hash chain may forbid it. Nothing was recorded."
            )
        if reason:
            note = f"{updated.get('narration') or ''}\nVoided: {reason}".strip()
            self.client.write(self.MODEL, invoice_id, {"narration": note})
            updated = self.get_invoice(invoice_id)
        logger.info("Voided invoice id=%d (reason=%r)", invoice_id, reason)
        return updated

    # ── QuickBooks reconciliation (drive the autolink module) ────────

    def push_qbo_update(self, invoice_id: int) -> dict:
        """Push a corrected posted invoice to QuickBooks now.

        Calls the ``atech_qbo_invoice_autolink`` module's manual
        ``action_qbo_push_update`` button. The invoice must be a posted
        customer invoice that was already exported to QuickBooks (has a
        mapping); the module raises a ``UserError`` otherwise.

        Args:
            invoice_id: The invoice (``account.move``) ID.

        Returns:
            The invoice record after the push was enqueued.
        """
        self.client.execute(self.MODEL, "action_qbo_push_update", [invoice_id])
        logger.info("Enqueued QBO update push for invoice id=%d", invoice_id)
        return self.get_invoice(invoice_id)

    def get_reconciliation_status(self, invoice_id: int) -> dict:
        """Report whether an invoice reconciles with QuickBooks.

        Combines the Odoo balance with the ``qbo.map.account.move`` self-heal
        state written by the reconciliation sweep, so a bookkeeper can see at
        a glance whether Odoo and QuickBooks agree on what is owed.

        Args:
            invoice_id: The invoice (``account.move``) ID.

        Returns:
            Dict with ``invoice`` (name/state/payment_state/residual), and
            ``qbo`` (``mapped``, and when mapped: ``qbo_id``,
            ``selfheal_attempts``, ``selfheal_last_attempt``,
            ``selfheal_escalated``). ``mapped: False`` means the invoice was
            never exported to QuickBooks.
        """
        inv = self.client.read(
            self.MODEL, invoice_id,
            fields=["name", "state", "payment_state", "amount_residual",
                    "amount_total"],
        )
        if not inv:
            return {"invoice": {}, "qbo": {"mapped": False}}
        mapping = self.client.search_read(
            "qbo.map.account.move",
            [["invoice_id", "=", invoice_id]],
            fields=["qbo_id", "atech_selfheal_attempts",
                    "atech_selfheal_last_attempt", "atech_selfheal_escalated"],
            limit=1,
        )
        qbo: dict[str, Any] = {"mapped": bool(mapping)}
        if mapping:
            m = mapping[0]
            qbo.update({
                "qbo_id": m.get("qbo_id"),
                "selfheal_attempts": m.get("atech_selfheal_attempts"),
                "selfheal_last_attempt": m.get("atech_selfheal_last_attempt"),
                "selfheal_escalated": m.get("atech_selfheal_escalated"),
            })
        return {"invoice": inv[0], "qbo": qbo}

    def run_qbo_selfheal(self) -> dict:
        """Run the Odoo↔QuickBooks payment self-heal sweep now.

        Triggers the ``cron_qbo_payment_selfheal`` scheduled action manually
        (``ir.cron.method_direct_trigger``) instead of waiting for its hourly
        run. The sweep re-imports payments for invoices QuickBooks considers
        settled but Odoo still shows open, converging on the invariant that
        the two systems agree on the balance owed.

        Returns:
            Dict with ``triggered: True`` and the cron ``cron_id``.
        """
        model, cron_id = self.client.execute(
            "ir.model.data", "check_object_reference",
            "atech_qbo_invoice_autolink", "cron_qbo_payment_selfheal",
        )
        self.client.execute("ir.cron", "method_direct_trigger", [cron_id])
        logger.info("Triggered QBO self-heal cron id=%d", cron_id)
        return {"triggered": True, "cron_id": cron_id}
