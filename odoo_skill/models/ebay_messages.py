"""
eBay buyer-message operations for the ``odoo-ebay-messages`` module
(Odoo 17 ``ebay.message`` / ``ebay.order``).

The module pulls buyer messages out of eBay into an Odoo inbox, threads them
(``ebay.message.line``), links them to the eBay order and Odoo product where
it can, drafts replies with OpenAI, and escalates into a helpdesk ticket.

Three things shape this class:

* **Sending is separated from drafting, deliberately.**
  ``action_generate_ai_reply`` only fills ``reply_draft``;
  ``action_send_inline_reply`` posts whatever sits in that draft to a real
  eBay buyer and clears it. Both are allowlisted, but
  :meth:`EbayMessageOps.draft_reply` never sends, and :meth:`send_reply`
  requires the caller to pass the body it is sending. An agent cannot
  generate-and-send in one step by accident, which matches how
  ``HelpdeskOps.draft_ai_reply`` treats the same risk.

* **``order_id`` cannot be filtered on.** On ``ebay.message`` it is computed
  with ``searchable: False`` — a domain on it is silently dropped and returns
  every message. Order lookups therefore go through the stored ``item_id``,
  or through :meth:`messages_for_order`, which resolves the order first and
  matches on that.

* **Replies leave an audit trail.** ``reply_log_ids`` records what was
  actually sent, which is the honest answer to "did we reply to this buyer",
  where ``status == 'replied'`` is only a flag.
"""

import logging
from datetime import timedelta
from typing import Any, Optional

from ._base import BaseOps, utc_stamp

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "subject", "sender", "status", "is_read", "user_id",
    "receive_date", "item_id", "ticket_id",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "body", "reply_draft", "external_message_id", "folder_id",
    "last_modified", "thread_ref_id", "line_ids", "reply_log_ids",
    "attachment_count", "buyer_message_count", "ebay_listing_url",
    "order_state", "order_tracking",
]

_LINE_FIELDS = ["id", "message_id", "create_date"]

_ORDER_FIELDS = [
    "id", "order_id", "buyer_user_id", "ebay_state", "order_status",
    "item_id", "item_name", "total", "currency_id", "created_time",
    "shipping_name", "shipping_city", "shipping_state", "shipping_postal",
    "sale_order_id", "order_link", "cancel_state", "fulfillment_state",
]

#: ``ebay.message.status`` values.
STATUSES = ["new", "replied", "closed"]

#: ``ebay.order.ebay_state`` values.
ORDER_STATES = ["new", "shipped", "canceled"]


class EbayMessageOps(BaseOps):
    """Operations on the eBay message inbox and its linked orders."""

    MODEL = "ebay.message"
    MODULE = "odoo-ebay-messages"
    ORDER_MODEL = "ebay.order"
    TEMPLATE_MODEL = "ebay.reply.template"
    LINE_MODEL = "ebay.message.line"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "receive_date desc"

    ALLOWED_ACTIONS = frozenset({
        # triage
        "action_mark_read",
        "action_mark_unread",
        "action_assign_to_me",
        "action_close",
        "action_reopen",
        # drafting (writes reply_draft, sends nothing)
        "action_generate_ai_reply",
        "action_revise_ai_reply",
        # sending — posts to a real buyer
        "action_send_inline_reply",
        # escalation / media
        "action_atech_create_ticket",
        "action_download_images",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def new_messages(self, limit: int = 50) -> list[dict]:
        """Buyer messages not yet replied to or closed."""
        return self.search([["status", "=", "new"]], limit=limit)

    def unread(self, limit: int = 50) -> list[dict]:
        """Messages nobody has opened."""
        return self.search(
            [["is_read", "=", False], ["status", "!=", "closed"]], limit=limit
        )

    def unassigned(self, limit: int = 50) -> list[dict]:
        """Open messages with no owner — the queue that goes stale quietest."""
        return self.search(
            [["status", "=", "new"], ["user_id", "=", False]], limit=limit
        )

    def assigned_to(self, user_id: int, limit: int = 50) -> list[dict]:
        """Open messages assigned to one agent."""
        return self.search(
            [["user_id", "=", user_id], ["status", "!=", "closed"]], limit=limit
        )

    def with_pending_draft(self, limit: int = 50) -> list[dict]:
        """Messages carrying an unsent reply draft awaiting review."""
        return self.search(
            [["reply_draft", "not in", [False, ""]], ["status", "!=", "closed"]],
            limit=limit,
        )

    def aging(self, older_than_hours: int = 24, limit: int = 50) -> list[dict]:
        """Unanswered messages older than *N* hours, oldest first.

        eBay grades sellers on response time, so this is the queue that
        actually costs money when it is ignored.
        """
        return self.search(
            self._aging_domain(older_than_hours), limit=limit,
            order="receive_date asc",
        )

    def _aging_domain(self, older_than_hours: int = 24) -> list:
        """Domain for the response-time queue — shared by the list and count."""
        return [
            ["status", "=", "new"],
            ["receive_date", "<=", utc_stamp(-timedelta(hours=older_than_hours))],
        ]

    def for_item(self, item_id: str, limit: int = 50) -> list[dict]:
        """Messages about one eBay item number."""
        return self.search([["item_id", "=", item_id]], limit=limit)

    def find_by_buyer(self, sender: str, limit: int = 50) -> list[dict]:
        """Every message from one buyer, newest first."""
        return self.search([["sender", "ilike", sender]], limit=limit)

    def messages_for_order(self, order_ref: str, limit: int = 50) -> list[dict]:
        """Messages tied to one eBay order number.

        ``ebay.message.order_id`` is computed and ``searchable: False``, so a
        domain on it is dropped and would return the whole inbox. The order is
        resolved first and its stored ``item_id`` used to narrow the scan.

        But ``item_id`` identifies the *listing*, not the order: a fixed-price
        listing sells many times, so narrowing on it alone returns every
        buyer's messages for that listing. That is worse than useless here —
        it silently mixes other customers' correspondence into an answer about
        one order. So ``order_id`` is read back per record and matched
        exactly, client-side, over the narrowed set.
        """
        orders = self.orders_by_number(order_ref)
        item_ids = [o["item_id"] for o in orders if o.get("item_id")]
        order_ids = {o["id"] for o in orders}
        if not item_ids:
            return []
        want = list(self.LIST_FIELDS)
        if "order_id" not in want:
            want.append("order_id")
        window = self._scan_window(limit)
        rows = self.client.search_read(
            self.MODEL, [["item_id", "in", item_ids]],
            fields=want, limit=window, order=self.ORDER,
        )
        if len(rows) >= window:
            logger.warning(
                "ebay.message: messages_for_order scanned the full %d-row "
                "window for item(s) %s; results may be incomplete.",
                window, ", ".join(item_ids),
            )
        matched = [r for r in rows if _ref_id(r.get("order_id")) in order_ids]
        return matched[:limit]

    def get_thread(self, message_id: int) -> dict:
        """Full conversation for a message, including what we actually sent."""
        record = self.get(message_id)
        logs = self.client.search_read(
            "ebay.message.reply.log", [["message_id", "=", message_id]],
            fields=["id", "create_date", "create_uid"], order="id",
        ) if record.get("reply_log_ids") else []
        return {
            "summary": (
                f"Message {message_id} from {record.get('sender')} — "
                f"{record.get('status')}, {len(logs)} reply/replies sent"
            ),
            "message": record,
            "replies_sent": logs,
        }

    def templates(self) -> list[dict]:
        """Canned reply templates, in display order."""
        self._require()
        return self.client.search_read(
            self.TEMPLATE_MODEL, [["active", "=", True]],
            fields=["id", "name", "body", "sequence"], order="sequence, id",
        )

    # ── Orders ───────────────────────────────────────────────────────

    def orders_by_number(self, order_ref: str) -> list[dict]:
        """Resolve an eBay order number **exactly**.

        Use this, not :meth:`find_order`, whenever the result authorises
        access to something. ``find_order`` is an ``ilike`` search built for
        human lookup, and substring matching is the wrong primitive for
        deciding whose data to return.
        """
        self._require()
        return self.client.search_read(
            self.ORDER_MODEL, [["order_id", "=", order_ref]],
            fields=_ORDER_FIELDS, limit=10, order="created_time desc",
        )

    def find_order(self, order_ref: str, limit: int = 10) -> list[dict]:
        """Locate eBay orders by order number, item number, or buyer.

        Fuzzy (``ilike``) — for human lookup only. Never use the result to
        decide what data a caller may see; see :meth:`orders_by_number`.
        """
        self._require()
        return self.client.search_read(
            self.ORDER_MODEL,
            ["|", "|",
             ["order_id", "ilike", order_ref],
             ["item_id", "ilike", order_ref],
             ["buyer_user_id", "ilike", order_ref]],
            fields=_ORDER_FIELDS, limit=limit, order="created_time desc",
        )

    def recent_orders(self, since_days: int = 7, limit: int = 50) -> list[dict]:
        """eBay orders created in the last *N* days."""
        self._require()
        return self.client.search_read(
            self.ORDER_MODEL,
            [["created_time", ">=", utc_stamp(-timedelta(days=since_days))]],
            fields=_ORDER_FIELDS, limit=limit, order="created_time desc",
        )

    def unshipped_orders(self, limit: int = 50) -> list[dict]:
        """Paid eBay orders not yet marked shipped, oldest first."""
        self._require()
        return self.client.search_read(
            self.ORDER_MODEL, [["ebay_state", "=", "new"]],
            fields=_ORDER_FIELDS, limit=limit, order="created_time asc",
        )

    def orders_without_sale_order(self, limit: int = 50) -> list[dict]:
        """eBay orders that never produced an Odoo sales order.

        Usually means the order-sync half of the bridge failed for that row —
        the money came in on eBay but nothing exists in Odoo to fulfil against.
        """
        self._require()
        return self.client.search_read(
            self.ORDER_MODEL,
            [["sale_order_id", "=", False], ["ebay_state", "!=", "canceled"]],
            fields=_ORDER_FIELDS, limit=limit, order="created_time desc",
        )

    # ── Triage ───────────────────────────────────────────────────────

    def assign(self, message_id: int, user_id: Optional[int] = None) -> dict:
        """Assign a message to an agent (default: the API user).

        Assignment also schedules the agent's "Reply to buyer" to-do via the
        model's ``write`` hook, so this is not just a label.
        """
        if user_id is None:
            return self.run_action(message_id, "action_assign_to_me")
        record = self.update(message_id, {"user_id": user_id})
        return {
            "summary": f"Message {message_id} assigned to user {user_id}",
            "message": record,
        }

    def mark_read(self, message_id: int) -> dict:
        """Mark a message read."""
        return self.run_action(message_id, "action_mark_read")

    def close(self, message_id: int) -> dict:
        """Close a conversation that needs no reply."""
        return self.run_action(message_id, "action_close")

    def reopen(self, message_id: int) -> dict:
        """Reopen a closed conversation back to New."""
        return self.run_action(message_id, "action_reopen")

    def create_ticket(self, message_id: int) -> dict:
        """Escalate a buyer message into a helpdesk ticket."""
        return self.run_action(message_id, "action_atech_create_ticket")

    # ── Drafting and sending ─────────────────────────────────────────

    def draft_reply(
        self, message_id: int, template_id: Optional[int] = None
    ) -> dict:
        """Generate an AI reply draft — does **not** send it.

        Feeds the whole thread to the model, not just the latest inbound
        message, because buyers often send several questions back-to-back.

        Args:
            message_id: Message to draft on.
            template_id: Optional canned template to seed the draft from.
        """
        if template_id:
            self.update(message_id, {"template_id": template_id})
        result = self.run_action(message_id, "action_generate_ai_reply")
        record = result["record"]
        return {
            "summary": (
                f"Draft generated for message {message_id} from "
                f"{record.get('sender')}. Nothing has been sent — review it, "
                "then send_reply explicitly."
            ),
            "draft": record.get("reply_draft"),
            "message": record,
        }

    def revise_draft(self, message_id: int, instruction: str) -> dict:
        """Rewrite an existing draft per an instruction — still does not send.

        Args:
            message_id: Message whose draft to revise.
            instruction: e.g. "make it shorter and more apologetic". The
                module raises if either the draft or the instruction is empty.
        """
        self.update(message_id, {"ai_revision_instruction": instruction})
        result = self.run_action(message_id, "action_revise_ai_reply")
        record = result["record"]
        return {
            "summary": f"Draft for message {message_id} revised. Still unsent.",
            "draft": record.get("reply_draft"),
            "message": record,
        }

    def send_reply(self, message_id: int, body: str) -> dict:
        """Send a reply to the eBay buyer. **This messages a real customer.**

        The body must be passed explicitly rather than read from whatever
        happens to be in ``reply_draft`` — so sending is always a decision
        about known text, never a blind flush of a draft some earlier step
        left behind. The text is written to the draft and then sent, which is
        exactly what the form's inline reply box does.

        Args:
            message_id: Message to reply to.
            body: The reply text to send.
        """
        if not (body or "").strip():
            raise ValueError("Refusing to send an empty reply to a buyer")
        self.update(message_id, {"reply_draft": body})
        result = self.run_action(message_id, "action_send_inline_reply")
        record = result["record"]
        return {
            "summary": (
                f"Reply sent to {record.get('sender')} on message {message_id}"
            ),
            "sent_body": body,
            "message": record,
        }

    # ── Summary ──────────────────────────────────────────────────────

    def inbox_summary(self) -> dict:
        """Inbox counts plus the response-time queue and order-sync gaps."""
        counts = {s: self.count([["status", "=", s]]) for s in STATUSES}
        unread = self.count(
            [["is_read", "=", False], ["status", "!=", "closed"]]
        )
        unassigned = self.count(
            [["status", "=", "new"], ["user_id", "=", False]]
        )
        aging = self.count(self._aging_domain(24))
        drafts = self.count(
            [["reply_draft", "not in", [False, ""]], ["status", "!=", "closed"]]
        )
        try:
            orphan_orders = self.client.search_count(
                self.ORDER_MODEL,
                [["sale_order_id", "=", False], ["ebay_state", "!=", "canceled"]],
            )
        except Exception:  # noqa: BLE001 — a summary must not fail on one count
            orphan_orders = -1
        return {
            "summary": (
                f"eBay inbox: {counts['new']} open ({unassigned} unassigned, "
                f"{unread} unread), {aging} waiting over 24h, "
                f"{drafts} drafts pending"
                + (f", {orphan_orders} orders with no Odoo sales order"
                   if orphan_orders > 0 else "")
            ),
            "by_status": counts,
            "unread": unread,
            "unassigned": unassigned,
            "aging_over_24h": aging,
            "drafts_pending": drafts,
            "orders_without_sale_order": orphan_orders,
        }


def _ref_id(value: Any) -> Any:
    """The id out of a many2one ``[id, name]`` pair, or None."""
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value or None
