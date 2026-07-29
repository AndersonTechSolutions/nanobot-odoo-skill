"""
Product-creation draft operations for ``quick_product`` and ``new_product_gui``.

Both modules stage a product behind an AI-assisted wizard before it becomes a
real catalogue record:

* ``quick.product.draft`` (``quick_product``) — a queued pipeline. State moves
  draft → queued → identifying → categorizing → dedup_review → pricing →
  awaiting_user → committing → committed, with ``failed`` and ``merged`` exits.
* ``product.creation.draft`` (``new_product_gui``) — an interactive OWL wizard.
  State is the step the user is on: entry → identify → dedup → categorize →
  attributes → pricing → review → done.

Both are AI-cost-bearing (``ai_cost_cents``) and both can stall waiting on a
human (``awaiting_user`` / ``dedup_review``). The most useful thing an agent
can do here is surface stalled and failed drafts — which is what
:meth:`ProductGuiOps.attention_needed` reports.

Committing a draft creates a permanent catalogue product, so ``action_commit``
is deliberately **not** allowlisted for unattended use; commit stays a human
action in the wizard.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_QP_LIST_FIELDS = [
    "id", "name", "title", "state", "condition", "sale_price",
    "cost_price", "ebay_title", "sku_preview", "ai_cost_cents",
    "committed_product_id", "create_date", "user_id",
]
_QP_DETAIL_FIELDS = _QP_LIST_FIELDS + [
    "description", "ai_identification", "ai_error", "confidence",
    "price_confidence", "price_confidence_pct",
    "price_source_label", "price_pick_tier",
    "suggested_price_min", "suggested_price_median", "suggested_price_max",
    "sold_comp_count", "active_comp_count", "price_comp_count",
    "dedup_decision", "dedup_selected_product_id", "match_product_ids",
    "needs_user_category_pick", "ebay_category_name", "odoo_category_id",
    "sell_as_lot", "lot_size", "publish_to_ebay", "low_confidence_note",
    "processing_started_at", "processing_worker",
]

_NPG_LIST_FIELDS = [
    "id", "name", "title", "state", "brand", "model", "grade",
    "sale_price", "cost_price", "suggested_price", "ai_cost_cents",
    "committed_product_id", "create_date",
]
_NPG_DETAIL_FIELDS = _NPG_LIST_FIELDS + [
    "notes", "ai_identification", "ai_confidence", "categ_id",
    "categ_confidence", "price_state", "price_error",
    "price_sold", "price_active", "price_ai", "price_ai_confidence",
    "price_sold_count", "price_active_count",
    "dedup_decision", "dedup_selected_product_id", "match_product_ids",
    "serial_no", "device_serial", "tracking", "initial_qty",
    "sell_as_lot", "lot_size", "lookup_state", "lookup_summary",
    "r2_cosmetic_grade", "r2_functional_grade", "r2_data_status",
]

#: quick.product.draft.state values.
QP_STATES = [
    "draft", "queued", "identifying", "categorizing", "dedup_review",
    "pricing", "awaiting_user", "committing", "committed", "merged", "failed",
]
#: quick.product.draft states that need a human before they can advance.
QP_STALLED = ["dedup_review", "awaiting_user", "failed"]

#: product.creation.draft.state values (wizard steps).
NPG_STATES = [
    "entry", "identify", "dedup", "categorize", "attributes",
    "pricing", "review", "done",
]


class ProductGuiOps(BaseOps):
    """Operations across both product-creation draft pipelines."""

    MODEL = "quick.product.draft"
    MODULE = "quick_product"
    NPG_MODEL = "product.creation.draft"
    LIST_FIELDS = _QP_LIST_FIELDS
    DETAIL_FIELDS = _QP_DETAIL_FIELDS
    ORDER = "create_date desc"

    ALLOWED_ACTIONS = frozenset({
        # re-drive a stuck or failed draft
        "action_retry",
        "action_rerun_ai",
        "action_run_ai_research",
        "action_queue",
        # pricing tier picks
        "action_pick_sold_med", "action_pick_sold_high", "action_pick_sold_aggr",
        "action_pick_active_med", "action_pick_active_high", "action_pick_active_aggr",
        "action_pick_ai_research",
        "action_use_suggested_price",
        "action_use_suggested_description",
        # abandon
        "action_cancel",
        "action_discard",
        # NOTE: action_commit is intentionally excluded — committing creates a
        # permanent catalogue product and stays a human action.
    })

    # ── Quick Product drafts ─────────────────────────────────────────

    def drafts_in_state(self, state: str, limit: int = 50) -> list[dict]:
        """Quick Product drafts in a given pipeline state."""
        if state not in QP_STATES:
            raise ValueError(f"state must be one of {QP_STATES}, got {state!r}")
        return self.search([["state", "=", state]], limit=limit)

    def stalled_drafts(self, limit: int = 50) -> list[dict]:
        """Drafts blocked on a human decision or outright failed."""
        return self.search([["state", "in", QP_STALLED]], limit=limit)

    def failed_drafts(self, limit: int = 50) -> list[dict]:
        """Drafts that errored out, with their AI error text."""
        return self.search(
            [["state", "=", "failed"]], limit=limit, fields=_QP_DETAIL_FIELDS
        )

    def low_confidence_drafts(self, threshold: float = 60.0, limit: int = 50) -> list[dict]:
        """Drafts whose pricing confidence is below *threshold* percent.

        These are the ones most likely to need a human to sanity-check the
        price before commit.

        Filters on the stored float ``price_confidence`` (0–1), not the
        non-stored display string ``price_confidence_pct``. *threshold* stays
        in percent for callers.
        """
        return self.search(
            [["price_confidence", "<", threshold / 100.0],
             ["price_confidence", ">", 0],
             ["state", "not in", ["committed", "merged", "failed"]]],
            limit=limit, fields=_QP_DETAIL_FIELDS,
        )

    # ── New Product GUI drafts ───────────────────────────────────────

    def npg_available(self) -> bool:
        """Whether ``new_product_gui`` is installed on this database."""
        try:
            self.client.fields_get(self.NPG_MODEL, attributes=["type"])
            return True
        except Exception:
            return False

    def npg_drafts(self, state: Optional[str] = None, limit: int = 50) -> list[dict]:
        """New Product GUI drafts, optionally filtered by wizard step."""
        if state and state not in NPG_STATES:
            raise ValueError(f"state must be one of {NPG_STATES}, got {state!r}")
        domain = [["state", "=", state]] if state else []
        return self.client.search_read(
            self.NPG_MODEL, domain, fields=_NPG_LIST_FIELDS,
            limit=limit, order="create_date desc",
        )

    def get_npg_draft(self, draft_id: int) -> dict:
        """Read one New Product GUI draft in detail."""
        rows = self.client.read(self.NPG_MODEL, [draft_id], fields=_NPG_DETAIL_FIELDS)
        if not rows:
            from ..errors import OdooRecordNotFoundError
            raise OdooRecordNotFoundError(
                f"No product.creation.draft with id {draft_id}"
            )
        return rows[0]

    def npg_incomplete(self, limit: int = 50) -> list[dict]:
        """NPG drafts left part-way through the wizard."""
        return self.client.search_read(
            self.NPG_MODEL, [["state", "not in", ["done"]]],
            fields=_NPG_LIST_FIELDS, limit=limit, order="create_date desc",
        )

    # ── Cross-pipeline ───────────────────────────────────────────────

    def attention_needed(self, limit: int = 50) -> dict:
        """Everything across both pipelines that a human needs to look at."""
        stalled = self.stalled_drafts(limit=limit)
        by_state: dict[str, list] = {}
        for d in stalled:
            by_state.setdefault(d["state"], []).append(d)

        npg_stuck: list[dict] = []
        if self.npg_available():
            npg_stuck = self.client.search_read(
                self.NPG_MODEL,
                [["state", "not in", ["done", "entry"]]],
                fields=_NPG_LIST_FIELDS, limit=limit,
            )

        parts = []
        for s in QP_STALLED:
            n = len(by_state.get(s, []))
            if n:
                parts.append(f"{n} {s}")
        if npg_stuck:
            parts.append(f"{len(npg_stuck)} NPG drafts mid-wizard")

        return {
            "summary": (
                "Product drafts needing attention: " + ", ".join(parts)
                if parts else "No product drafts need attention."
            ),
            "quick_product": by_state,
            "new_product_gui": npg_stuck,
            "total": len(stalled) + len(npg_stuck),
        }

    def ai_spend_summary(self) -> dict:
        """Total AI cost accrued across drafts, in dollars.

        ``ai_cost_cents`` accumulates per draft; this is the cheapest way to
        answer "what has the product pipeline cost us".
        """
        rows = self.client.search_read(
            self.MODEL, [["ai_cost_cents", ">", 0]],
            fields=["id", "ai_cost_cents", "state"], limit=5000,
        )
        total = sum(r.get("ai_cost_cents") or 0 for r in rows)
        committed = sum(
            r.get("ai_cost_cents") or 0 for r in rows if r.get("state") == "committed"
        )
        wasted = total - committed
        return {
            "summary": (
                f"Quick Product AI spend: ${total / 100:.2f} across {len(rows)} drafts "
                f"(${committed / 100:.2f} on committed, ${wasted / 100:.2f} on "
                f"uncommitted/failed)"
            ),
            "total_usd": round(total / 100, 2),
            "committed_usd": round(committed / 100, 2),
            "uncommitted_usd": round(wasted / 100, 2),
            "draft_count": len(rows),
        }

    def pipeline_summary(self) -> dict:
        """Draft counts by state across both pipelines."""
        qp = {s: self.count([["state", "=", s]]) for s in QP_STATES}
        out: dict[str, Any] = {
            "quick_product_by_state": qp,
            "quick_product_stalled": sum(qp[s] for s in QP_STALLED),
        }
        if self.npg_available():
            npg = {
                s: self.client.search_count(self.NPG_MODEL, [["state", "=", s]])
                for s in NPG_STATES
            }
            out["new_product_gui_by_state"] = npg
            out["new_product_gui_open"] = sum(
                v for k, v in npg.items() if k != "done"
            )
        out["summary"] = (
            f"Product drafts: {qp['committed']} committed, "
            f"{out['quick_product_stalled']} stalled"
            + (f", {out.get('new_product_gui_open', 0)} NPG in progress"
               if "new_product_gui_open" in out else "")
        )
        return out
