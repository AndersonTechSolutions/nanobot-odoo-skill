"""
Auction sourcing operations for the ``auction_scrapper_catalog`` module
(Odoo 17 ``auction.lot`` / ``auction.auction`` / ``auction.watchlist``).

The module scrapes third-party auction sites for equipment worth buying,
matches discovered lots against keyword watchlists, and tracks bidding
approval. Lots move ``new -> waiting -> enriched -> ending_soon -> ended``,
with ``refreshing`` while a re-scrape is in flight and ``error`` when one
fails.

Two things shape this class:

* **Bid approval is a two-field decision.** ``approved_for_bidding`` and
  ``approved_max_bid`` only mean something together — an approved lot with no
  ceiling is an open chequebook, and a ceiling on an unapproved lot does
  nothing. :meth:`AuctionOps.approve_bid` writes both and refuses a
  non-positive ceiling; :meth:`revoke_approval` clears both.

* **Soft close makes end times move.** These sites extend a lot's end time
  whenever a late bid lands (``extension_count``, ``last_extended_at``), so
  ``current_end_at`` is the live deadline and ``original_end_at`` is only
  history. Every time-based query here reads ``current_end_at``.

This module is **staging-only** at the time of writing — it is not installed
on production. The base class raises :class:`OdooModuleNotInstalledError`
with the module name when it is absent, so calling into it elsewhere gives a
clear answer rather than a fault.
"""

import logging
from datetime import timedelta
from typing import Any, Optional

from ._base import BaseOps, utc_stamp

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "title", "lot_number", "site_code", "auction_id", "current_bid",
    "current_end_at", "state", "watchlist_state", "approved_for_bidding",
    "approved_max_bid", "match_relevance",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "description", "condition_text", "category_path", "starting_bid",
    "bid_count", "currency", "source_url", "source_lot_id", "original_end_at",
    "extension_count", "last_extended_at", "discovered_via",
    "matched_watchlist_ids", "tag_ids", "notes", "photo_urls",
    "last_writeback_at",
]

_AUCTION_FIELDS = [
    "id", "name", "site_code", "auctioneer_name", "state", "start_at",
    "end_at", "lot_count", "location_city", "location_state",
    "buyer_premium_pct", "soft_close_window_min", "terms_doc_url",
]

_WATCHLIST_FIELDS = ["id", "name", "slug", "active", "last_sync_at", "color"]

#: ``auction.lot.state`` values, in lifecycle order.
LOT_STATES = [
    "new", "waiting", "enriched", "refreshing", "ending_soon",
    "ended_pending_final", "ended", "withdrawn", "error",
]

#: Lot states where the lot is still biddable.
LIVE_STATES = ["new", "waiting", "enriched", "refreshing", "ending_soon"]

#: ``watchlist_state`` values.
WATCH_STATES = ["none", "watching", "dismissed"]

#: ``auction.auction.state`` values.
AUCTION_STATES = ["upcoming", "live", "ended", "archived"]


class AuctionOps(BaseOps):
    """Operations on scraped auction lots, auctions, and watchlists."""

    MODEL = "auction.lot"
    MODULE = "auction_scrapper_catalog"
    AUCTION_MODEL = "auction.auction"
    WATCHLIST_MODEL = "auction.watchlist"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "current_end_at asc"

    ALLOWED_ACTIONS = frozenset({
        "action_rescrape",
        "action_relogin",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def ending_soon(self, within_hours: int = 6, limit: int = 50) -> list[dict]:
        """Live lots closing within *N* hours, soonest first.

        Reads ``current_end_at`` — soft close pushes that forward on every
        late bid, so ``original_end_at`` would under-report what is still open.
        """
        return self.search(
            [["state", "in", LIVE_STATES],
             ["current_end_at", "!=", False],
             ["current_end_at", "<=", utc_stamp(timedelta(hours=within_hours))]],
            limit=limit, order="current_end_at asc",
        )

    def watching(self, limit: int = 50) -> list[dict]:
        """Lots explicitly marked as being watched."""
        return self.search(
            [["watchlist_state", "=", "watching"], ["state", "in", LIVE_STATES]],
            limit=limit,
        )

    def approved_for_bidding(self, limit: int = 50) -> list[dict]:
        """Lots cleared to bid on, with their ceilings."""
        return self.search(
            [["approved_for_bidding", "=", True], ["state", "in", LIVE_STATES]],
            limit=limit,
        )

    def needs_approval(self, limit: int = 50) -> list[dict]:
        """Watched lots nobody has approved a bid ceiling for yet."""
        return self.search(
            [["watchlist_state", "=", "watching"],
             ["approved_for_bidding", "=", False],
             ["state", "in", LIVE_STATES]],
            limit=limit,
        )

    def over_ceiling(self, limit: int = 50) -> list[dict]:
        """Approved lots whose current bid has passed the approved maximum.

        Comparing two fields to each other is not expressible in an Odoo
        domain, so this runs client-side over the approved set — which is
        small by construction.
        """
        return self.search_computed(
            [["approved_for_bidding", "=", True], ["state", "in", LIVE_STATES]],
            lambda r: (r.get("current_bid") or 0) > (r.get("approved_max_bid") or 0),
            limit=limit, extra_fields=["current_bid", "approved_max_bid"],
        )

    def high_relevance(self, min_relevance: int = 50, limit: int = 50) -> list[dict]:
        """Live lots the matcher scored highly, best first — the triage queue."""
        return self.search(
            [["match_relevance", ">=", min_relevance],
             ["state", "in", LIVE_STATES],
             ["watchlist_state", "=", "none"]],
            limit=limit, order="match_relevance desc, current_end_at asc",
        )

    def lots_for_watchlist(self, watchlist_id: int, limit: int = 50) -> list[dict]:
        """Live lots matched by one watchlist."""
        return self.search(
            [["matched_watchlist_ids", "in", [watchlist_id]],
             ["state", "in", LIVE_STATES]],
            limit=limit,
        )

    def find_lot(self, query: str, limit: int = 20) -> list[dict]:
        """Locate lots by title, lot number, or source id."""
        return self.search(
            ["|", "|",
             ["title", "ilike", query],
             ["lot_number", "ilike", query],
             ["source_lot_id", "ilike", query]],
            limit=limit,
        )

    def errored_lots(self, limit: int = 50) -> list[dict]:
        """Lots whose scrape failed — the scraper-health queue."""
        return self.search([["state", "=", "error"]], limit=limit, order="id desc")

    def auctions(self, state: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Auctions, optionally filtered to one state.

        Args:
            state: One of :data:`AUCTION_STATES`, or ``None`` for all.
        """
        self._require()
        if state and state not in AUCTION_STATES:
            raise ValueError(
                f"state must be one of {AUCTION_STATES}, got {state!r}"
            )
        domain = [["state", "=", state]] if state else []
        return self.client.search_read(
            self.AUCTION_MODEL, domain, fields=_AUCTION_FIELDS,
            limit=limit, order="end_at asc",
        )

    def watchlists(self) -> list[dict]:
        """Active watchlists with their keyword and category-prefix counts."""
        self._require()
        rows = self.client.search_read(
            self.WATCHLIST_MODEL, [["active", "=", True]],
            fields=_WATCHLIST_FIELDS + ["keyword_ids", "category_prefix_ids"],
            order="name",
        )
        for row in rows:
            row["keyword_count"] = len(row.pop("keyword_ids", []) or [])
            row["category_prefix_count"] = len(
                row.pop("category_prefix_ids", []) or []
            )
        return rows

    # ── Writes ───────────────────────────────────────────────────────

    def set_watching(self, lot_id: int) -> dict:
        """Mark a lot as being watched."""
        record = self.update(lot_id, {"watchlist_state": "watching"})
        return {
            "summary": f"Watching lot '{record['title']}'",
            "lot": record,
        }

    def set_dismissed(self, lot_id: int) -> dict:
        """Dismiss a lot so it stops surfacing in triage queues."""
        record = self.update(lot_id, {"watchlist_state": "dismissed"})
        return {
            "summary": f"Dismissed lot '{record['title']}'",
            "lot": record,
        }

    def approve_bid(self, lot_id: int, max_bid: float) -> dict:
        """Clear a lot for bidding up to a ceiling.

        Both fields are written together: an approval with no ceiling is an
        open chequebook, and the module's own board reads them as a pair.
        Also marks the lot watched, since approving something you are not
        watching leaves it out of every follow-up queue.

        Args:
            lot_id: Lot to approve.
            max_bid: Ceiling. Must be positive.
        """
        if max_bid is None or float(max_bid) <= 0:
            raise ValueError(
                "max_bid must be a positive ceiling — approving with no limit "
                "is not supported"
            )
        record = self.update(lot_id, {
            "approved_for_bidding": True,
            "approved_max_bid": float(max_bid),
            "watchlist_state": "watching",
        })
        current = record.get("current_bid") or 0
        note = (
            f" — note the current bid ({current}) is already at or above this "
            "ceiling" if current >= float(max_bid) else ""
        )
        return {
            "summary": (
                f"Lot '{record['title']}' approved to bid up to {max_bid}{note}"
            ),
            "lot": record,
        }

    def revoke_approval(self, lot_id: int) -> dict:
        """Withdraw bidding approval and clear the ceiling."""
        record = self.update(lot_id, {
            "approved_for_bidding": False,
            "approved_max_bid": 0.0,
        })
        return {
            "summary": f"Bidding approval revoked on '{record['title']}'",
            "lot": record,
        }

    def set_notes(self, lot_id: int, notes: str) -> dict:
        """Record an internal note on a lot."""
        record = self.update(lot_id, {"notes": notes})
        return {"summary": f"Note saved on '{record['title']}'", "lot": record}

    def rescrape(self, lot_id: int) -> dict:
        """Re-fetch a lot from its source site."""
        return self.run_action(lot_id, "action_rescrape")

    # ── Summary ──────────────────────────────────────────────────────

    def sourcing_summary(self) -> dict:
        """The bidding picture: what is live, watched, approved, and closing."""
        live = self.count([["state", "in", LIVE_STATES]])
        watched = self.count(
            [["watchlist_state", "=", "watching"], ["state", "in", LIVE_STATES]]
        )
        approved = self.count(
            [["approved_for_bidding", "=", True], ["state", "in", LIVE_STATES]]
        )
        pending = self.count(
            [["watchlist_state", "=", "watching"],
             ["approved_for_bidding", "=", False],
             ["state", "in", LIVE_STATES]]
        )
        closing = len(self.ending_soon(within_hours=6, limit=200))
        errored = self.count([["state", "=", "error"]])
        over = self.count_computed(
            [["approved_for_bidding", "=", True], ["state", "in", LIVE_STATES]],
            lambda r: (r.get("current_bid") or 0) > (r.get("approved_max_bid") or 0),
            extra_fields=["current_bid", "approved_max_bid"],
        )
        return {
            "summary": (
                f"Auctions: {live} live lots, {watched} watched, "
                f"{approved} approved to bid ({over} now over ceiling), "
                f"{pending} awaiting approval, {closing} closing within 6h"
                + (f", {errored} scrape errors" if errored else "")
            ),
            "live_lots": live,
            "watching": watched,
            "approved": approved,
            "over_ceiling": over,
            "awaiting_approval": pending,
            "closing_6h": closing,
            "scrape_errors": errored,
        }
