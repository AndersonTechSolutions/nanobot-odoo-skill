"""
Messaging operations for the ``atech_messaging`` module family.

``atech_messaging`` provides SMS/RCS over a custom Twilio REST integration;
``atech_messaging_ebay`` and ``atech_messaging_meta`` add eBay and Meta
(Facebook/Instagram/WhatsApp) transports behind the same conversation model.

Model roles:

* ``atech.inbox`` — a channel endpoint (e.g. a Twilio number).
* ``atech.conversation`` — a thread with one contact on one channel.
* ``atech.message`` — an individual inbound/outbound message.

Replies go through ``action_reply``, which reads a composer record rather
than taking a body argument, so :meth:`MessagingOps.reply` creates the
message directly on the conversation instead — the transport picks it up
from ``state``.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_CONV_LIST_FIELDS = [
    "id", "name", "channel", "status", "contact_label", "contact_number",
    "partner_id", "assigned_agent_id", "unread_count", "last_message_on",
    "last_message_preview", "is_spam",
]
_CONV_DETAIL_FIELDS = _CONV_LIST_FIELDS + [
    "inbox_id", "label_ids", "snoozed_until", "res_model", "res_id",
    "meta_external_id", "sms_message_ids",
]

_MSG_FIELDS = [
    "id", "conversation_id", "channel", "direction", "body", "number",
    "partner_id", "state", "error", "create_date", "scheduled_for",
]

_INBOX_FIELDS = ["id", "name", "channel", "twilio_number", "default_agent_id", "active"]

#: atech.conversation.status values.
STATUSES = ["open", "pending", "resolved", "snoozed"]

#: Transports a conversation can arrive on.
CHANNELS = ["sms", "rcs", "whatsapp", "ebay", "facebook", "instagram"]


class MessagingOps(BaseOps):
    """Operations on customer conversations across SMS/RCS/eBay/Meta."""

    MODEL = "atech.conversation"
    MODULE = "atech_messaging"
    MESSAGE_MODEL = "atech.message"
    INBOX_MODEL = "atech.inbox"
    LIST_FIELDS = _CONV_LIST_FIELDS
    DETAIL_FIELDS = _CONV_DETAIL_FIELDS
    ORDER = "last_message_on desc"

    ALLOWED_ACTIONS = frozenset({
        "action_set_open",
        "action_set_pending",
        "action_set_resolved",
        "action_mark_read",
        "action_assign_me",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def open_conversations(self, limit: int = 50) -> list[dict]:
        """Conversations needing attention (open or pending, not spam)."""
        return self.search(
            [["status", "in", ["open", "pending"]], ["is_spam", "=", False]],
            limit=limit,
        )

    def unread(self, limit: int = 50) -> list[dict]:
        """Conversations with unread inbound messages."""
        return self.search(
            [["unread_count", ">", 0], ["is_spam", "=", False]], limit=limit
        )

    def conversations_on(self, channel: str, limit: int = 50) -> list[dict]:
        """Conversations on one transport."""
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {CHANNELS}, got {channel!r}")
        return self.search([["channel", "=", channel]], limit=limit)

    def find_by_contact(self, query: str, limit: int = 20) -> list[dict]:
        """Find conversations by contact number or label."""
        return self.search(
            ["|", ["contact_number", "ilike", query],
                  ["contact_label", "ilike", query]],
            limit=limit,
        )

    def assigned_to(self, user_id: int, limit: int = 50) -> list[dict]:
        """Conversations assigned to an agent."""
        return self.search(
            [["assigned_agent_id", "=", user_id],
             ["status", "in", ["open", "pending"]]],
            limit=limit,
        )

    def get_messages(self, conversation_id: int, limit: int = 100) -> list[dict]:
        """Read a conversation's message history, oldest first."""
        self._require()
        return self.client.search_read(
            self.MESSAGE_MODEL, [["conversation_id", "=", conversation_id]],
            fields=_MSG_FIELDS, limit=limit, order="create_date asc",
        )

    def get_thread(self, conversation_id: int, limit: int = 100) -> dict:
        """Conversation detail plus its messages."""
        record = self.get(conversation_id)
        record["messages"] = self.get_messages(conversation_id, limit=limit)
        return record

    def inboxes(self) -> list[dict]:
        """Configured channel endpoints."""
        self._require()
        return self.client.search_read(
            self.INBOX_MODEL, [["active", "=", True]], fields=_INBOX_FIELDS, limit=50
        )

    def failed_messages(self, limit: int = 50) -> list[dict]:
        """Outbound messages that errored — a delivery-health check."""
        self._require()
        return self.client.search_read(
            self.MESSAGE_MODEL,
            [["direction", "=", "out"], ["error", "!=", False]],
            fields=_MSG_FIELDS, limit=limit, order="create_date desc",
        )

    # ── Writes ───────────────────────────────────────────────────────

    def reply(
        self,
        conversation_id: int,
        body: str,
        scheduled_for: Optional[str] = None,
    ) -> dict:
        """Queue an outbound reply on a conversation.

        The message is created against the conversation and left for the
        transport to pick up. ``channel``, ``number`` and ``partner_id`` are
        inherited from the conversation so the reply goes back out on the
        same transport it arrived on.

        Args:
            conversation_id: Thread to reply on.
            body: Message text.
            scheduled_for: Optional ``YYYY-MM-DD HH:MM:SS`` to send later.
        """
        conv = self.get(conversation_id)
        values: dict[str, Any] = {
            "conversation_id": conversation_id,
            "channel": conv.get("channel"),
            "direction": "out",
            "body": body,
        }
        if conv.get("contact_number"):
            values["number"] = conv["contact_number"]
        partner = conv.get("partner_id")
        if partner:
            values["partner_id"] = partner[0] if isinstance(partner, (list, tuple)) else partner
        if scheduled_for:
            values["scheduled_for"] = scheduled_for

        msg_id = self.client.create(self.MESSAGE_MODEL, values)
        rows = self.client.read(self.MESSAGE_MODEL, [msg_id], fields=_MSG_FIELDS)
        return {
            "summary": (
                f"Reply queued on conversation {conversation_id} "
                f"({conv.get('channel')} → {conv.get('contact_label') or conv.get('contact_number')})"
            ),
            "message": rows[0] if rows else {"id": msg_id},
        }

    def assign(self, conversation_id: int, user_id: int) -> dict:
        """Assign a conversation to an agent."""
        record = self.update(conversation_id, {"assigned_agent_id": user_id})
        return {
            "summary": f"Conversation {conversation_id} assigned",
            "conversation": record,
        }

    def mark_spam(self, conversation_id: int, is_spam: bool = True) -> dict:
        """Flag or unflag a conversation as spam."""
        record = self.update(conversation_id, {"is_spam": is_spam})
        return {
            "summary": f"Conversation {conversation_id} "
                       f"{'marked as spam' if is_spam else 'unmarked as spam'}",
            "conversation": record,
        }

    # ── Summary ──────────────────────────────────────────────────────

    def inbox_summary(self) -> dict:
        """Per-status and per-channel counts — the messaging desk view."""
        by_status = {
            s: self.count([["status", "=", s], ["is_spam", "=", False]])
            for s in STATUSES
        }
        by_channel = {
            c: self.count([["channel", "=", c], ["status", "in", ["open", "pending"]]])
            for c in CHANNELS
        }
        unread_convs = self.count(
            [["unread_count", ">", 0], ["is_spam", "=", False]]
        )
        return {
            "summary": (
                f"Messaging: {by_status['open']} open, "
                f"{by_status['pending']} pending, "
                f"{unread_convs} with unread messages"
            ),
            "by_status": by_status,
            "by_channel": {k: v for k, v in by_channel.items() if v},
            "unread_conversations": unread_convs,
        }
