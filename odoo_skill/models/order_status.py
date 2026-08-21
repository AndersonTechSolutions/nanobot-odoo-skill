"""
Customer order-status operations for the ``atech_order_status`` module
(Odoo 17 ``sale.order`` extensions).

The module gives every sales order a private capability token
(``status_token``, ``secrets.token_urlsafe(24)``, stamped server-side on
create) backing a public status page the customer reaches without an Odoo
portal login, plus a branded confirmation email sent once on confirm
(``order_confirmation_sent`` guards the re-send) and a signature-capture flow
for quotations.

Three things shape this class:

* **The token is a secret, so it is not in the field lists.** Anything that
  reads a token hands out the capability, and a chat agent that lists orders
  should not be spraying live customer links into a transcript. ``status_token``
  is excluded from :data:`_LIST_FIELDS` and :data:`_DETAIL_FIELDS`; the link
  is produced only by :meth:`OrderStatusOps.status_link`, one order at a time,
  because you asked for that order's link.

* **The URL is built client-side.** The module's ``_get_status_url`` is
  private, and Odoo refuses RPC calls to methods starting with ``_``. So this
  mirrors its logic — including the https-only guard, which exists so a typo
  in the settings URL cannot leak the token to the wrong host.

* **A captured signature is immutable.** The model's ``write`` override
  refuses to change ``signature`` / ``signed_by`` / ``signed_on`` once the
  order has left quotation state. That guard is deliberate (it backstops the
  view's readonly modifiers against direct RPC writes), so this class offers
  no signature setter at all — capture belongs in the signature pad UI.
"""

import logging
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from ..errors import OdooError
from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

#: Deliberately excludes ``status_token`` — see the module docstring.
_LIST_FIELDS = [
    "id", "name", "partner_id", "state", "amount_total", "date_order",
    "order_confirmation_sent", "signed_by", "signed_on",
]

# NOTE: sale.order carries no partner_email / partner_phone fields — contact
# details live on the partner. Naming them here makes every get() raise, since
# read() rejects unknown fields outright rather than skipping them.
_DETAIL_FIELDS = _LIST_FIELDS + [
    "commitment_date", "user_id", "delivery_status", "invoice_status", "note",
]

#: ir.config_parameter holding the public status-page base URL.
STATUS_PAGE_PARAM = "atech_order_status.status_page_url"

#: ir.config_parameter gating the auto-send of the confirmation email.
AUTO_SEND_PARAM = "atech_order_status.auto_send_confirmation"

#: Quotation states — the only ones where a signature may still be captured.
QUOTATION_STATES = ["draft", "sent"]


class OrderStatusOps(BaseOps):
    """Operations on the customer-facing status/signature side of sales orders."""

    MODEL = "sale.order"
    MODULE = "atech_order_status"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "date_order desc"

    #: No button methods — the module extends ``action_confirm`` rather than
    #: adding its own, and confirming an order belongs to the sales ops class.
    ALLOWED_ACTIONS = frozenset()

    def available(self) -> bool:
        """Whether ``atech_order_status`` is installed.

        ``sale.order`` exists everywhere, so the base check on :attr:`MODEL`
        would always pass. The discriminator is the field the module adds.
        """
        if self._available is None:
            try:
                fields = self.client.fields_get(self.MODEL, attributes=["type"])
                self._available = "status_token" in fields
            except OdooError:
                self._available = False
            if not self._available:
                logger.info("atech_order_status not installed (no status_token)")
        return self._available

    # ── Settings ─────────────────────────────────────────────────────

    def _config_param(self, key: str, default: str = "") -> str:
        """Read an ``ir.config_parameter``, tolerating a locked-down API user.

        Reading config parameters normally needs Settings access. A user that
        lacks it should get an empty answer and a logged note, not a fault
        surfacing from what looks like an unrelated call.
        """
        try:
            rows = self.client.search_read(
                "ir.config_parameter", [["key", "=", key]],
                fields=["value"], limit=1,
            )
        except OdooError as exc:
            logger.info("Cannot read config parameter %s: %s", key, exc)
            return default
        return (rows[0]["value"] if rows else default) or default

    def settings(self) -> dict:
        """Report how the status page is configured on this database."""
        base = self._config_param(STATUS_PAGE_PARAM).strip()
        auto = self._config_param(AUTO_SEND_PARAM, "True")
        auto_on = auto not in ("False", "0", "", "false")
        parts = urlsplit(base) if base else None
        https_ok = bool(parts and parts.scheme == "https" and parts.netloc)
        problems = []
        if not base:
            problems.append(
                f"'{STATUS_PAGE_PARAM}' is unset — status links cannot be built."
            )
        elif not https_ok:
            problems.append(
                f"'{STATUS_PAGE_PARAM}' is not a valid https URL ({base!r}); "
                "the module refuses to emit a link over plain http so a typo "
                "cannot leak the token."
            )
        return {
            "summary": (
                f"Status page: {base or '(unset)'}; "
                f"auto-send confirmation {'on' if auto_on else 'off'}"
                + ("; " + " ".join(problems) if problems else "")
            ),
            "status_page_url": base,
            "auto_send_confirmation": auto_on,
            "usable": https_ok,
            "problems": problems,
        }

    # ── Links ────────────────────────────────────────────────────────

    def status_link(self, order_id: int) -> dict:
        """Build the customer status link for one order.

        Mirrors the module's private ``_get_status_url``: the base URL comes
        from settings, ``ref`` is the order name and ``t`` the capability
        token, and a non-https base yields no link at all.

        The returned URL contains a live secret. It is meant to be handed to
        that order's customer — do not post it anywhere else.
        """
        self._require()
        rows = self.client.read(
            self.MODEL, [order_id], fields=["name", "status_token"]
        )
        if not rows:
            raise ValueError(f"No sale.order with id {order_id}")
        order = rows[0]
        base = self._config_param(STATUS_PAGE_PARAM).strip()
        name = order.get("name")
        token = order.get("status_token")

        if not base or not token or not name:
            missing = (
                "the status-page URL is unset" if not base
                else "the order has no status token" if not token
                else "the order has no reference"
            )
            return {
                "summary": f"No status link for order {order_id} — {missing}.",
                "url": None,
                "order": name,
            }

        parts = urlsplit(base)
        if parts.scheme != "https" or not parts.netloc:
            return {
                "summary": (
                    f"No status link for {name} — the configured base URL "
                    f"({base!r}) is not https, and the module refuses to emit "
                    "a token over plain http."
                ),
                "url": None,
                "order": name,
            }

        query = parse_qsl(parts.query, keep_blank_values=True)
        query += [("ref", name), ("t", token)]
        url = urlunsplit((
            parts.scheme, parts.netloc, parts.path,
            urlencode(query, quote_via=quote), parts.fragment,
        ))
        return {
            "summary": (
                f"Status link for {name} (contains a private token — send it "
                "only to that customer)"
            ),
            "url": url,
            "order": name,
        }

    # ── Reads ────────────────────────────────────────────────────────

    def awaiting_signature(self, limit: int = 50) -> list[dict]:
        """Quotations sent to a customer but not yet signed."""
        return self.search(
            [["state", "in", QUOTATION_STATES], ["signature", "=", False]],
            limit=limit,
        )

    def signed_quotations(self, limit: int = 50) -> list[dict]:
        """Quotations carrying a customer signature, newest first."""
        return self.search(
            [["state", "in", QUOTATION_STATES], ["signature", "!=", False]],
            limit=limit, order="signed_on desc",
        )

    def confirmation_not_sent(self, limit: int = 50) -> list[dict]:
        """Confirmed orders whose branded confirmation email never went out.

        The send is best-effort by design — ``_send_order_confirmation_email``
        logs and swallows failures so a mail problem never blocks a
        confirmation. That makes this the queue where those failures surface,
        along with orders whose customer has no email address.
        """
        return self.search(
            [["state", "in", ["sale", "done"]],
             ["order_confirmation_sent", "=", False]],
            limit=limit,
        )

    def missing_email(self, limit: int = 50) -> list[dict]:
        """Confirmed orders whose customer has no email — never confirmable by mail."""
        return self.search(
            [["state", "in", ["sale", "done"]],
             ["partner_id.email", "=", False]],
            limit=limit,
        )

    def find_by_token(self, token: str) -> list[dict]:
        """Resolve a status token back to its order — for support triage.

        A customer quoting a link they were sent is the usual reason to need
        this. Exact match only: a token is a capability, not a search term,
        and an ``ilike`` prefix search over tokens would be an oracle.
        """
        return self.search([["status_token", "=", token]], limit=1)

    # ── Summary ──────────────────────────────────────────────────────

    def status_summary(self) -> dict:
        """Signature and confirmation-email queues, plus config health."""
        quotes_open = self.count([["state", "in", QUOTATION_STATES]])
        unsigned = self.count(
            [["state", "in", QUOTATION_STATES], ["signature", "=", False]]
        )
        unsent = self.count(
            [["state", "in", ["sale", "done"]],
             ["order_confirmation_sent", "=", False]]
        )
        config = self.settings()
        return {
            "summary": (
                f"Order status: {quotes_open} open quotations ({unsigned} unsigned), "
                f"{unsent} confirmed orders without a confirmation email sent. "
                f"Status page {'configured' if config['usable'] else 'NOT usable'}."
            ),
            "open_quotations": quotes_open,
            "awaiting_signature": unsigned,
            "confirmation_not_sent": unsent,
            "config": config,
        }
