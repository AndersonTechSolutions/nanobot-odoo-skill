"""Tests for the ``--confirm`` write gate in ``odoo.py``.

``_op_writes`` decides whether a generic ``call <ns>.<method>`` may run
unattended or must be re-issued with ``--confirm``. It is a name heuristic, so
it drifts silently as methods are added: a new read that happens to start with
a write verb becomes annoying, and — much worse — a new write that does not
becomes ungated.

The failure that motivated these tests: ``marketplace_summary`` is a read, but
a bare ``startswith("mark")`` classified it as a write, while ``assigned_to``
(a read) was gated by ``startswith("assign")`` and bare ``update`` — a write
inherited by *every* namespace — was not gated at all once the prefixes were
underscore-terminated. Both directions are asserted below.

The sweep at the end walks every method actually exposed on every namespace and
compares it against a frozen inventory, so a newly added method has to be
classified explicitly rather than defaulting to "read" unnoticed. That
inventory replaced a substring heuristic which had itself missed two
side-effecting reads — see EXPECTED_WRITES.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import odoo  # noqa: E402
from odoo_skill.client import OdooClient  # noqa: E402
from odoo_skill.config import OdooConfig  # noqa: E402
from odoo_skill.smart_actions import SmartActionHandler  # noqa: E402


#: Names that MUST require --confirm. Bare verbs first — those are the ones an
#: underscore-terminated prefix silently misses.
WRITES = [
    "create", "update", "unlink", "assign", "reply", "close", "reopen",
    "publish", "unpublish", "schedule", "reschedule", "unschedule", "rescrape",
    "create_repair", "update_task", "delete_customer", "add_part", "set_price",
    "post_invoice", "mark_sold", "mark_listed", "mark_renewed", "end_listing",
    "apply_suggested_price", "record_tracking", "run_action", "smart_create_lead",
    "cancel_order", "start_task", "complete_task", "submit_expense",
    "toggle_checklist_item", "approve_leave", "approve_bid", "revoke_approval",
    "confirm_receipt", "confirm_order", "receive_line", "flag_exception",
    "note_line", "send_reply", "revise_draft", "log_time", "move_stage",
    "remove_component", "generate_content", "reset_draft", "close_session",
    "save_as_catalog", "find_or_create_partner", "draft_reply", "draft_ai_reply",
    "set_watching", "set_dismissed",
]

#: Names that MUST NOT require --confirm — reads that begin with a write verb.
READS = [
    "marketplace_summary", "assigned_to", "unassigned", "settings",
    "scheduled_jobs", "unscheduled_jobs", "draft_listings", "draft_builds",
    "approved_for_bidding", "renewal_due", "watching", "watchlists",
    "confirmation_not_sent", "ending_soon", "escalated_tickets",
    "pending_ai_drafts", "with_pending_draft", "awaiting_confirmation",
    "catalog_builds", "open_sessions", "ready_to_receive", "stalled_drafts",
    "lots_for_watchlist", "bench_summary", "get_lines", "find_by_serial",
]


@pytest.mark.parametrize("name", WRITES)
def test_write_methods_require_confirm(name):
    assert odoo._op_writes(name) is True, (
        f"{name!r} mutates data but would run without --confirm"
    )


@pytest.mark.parametrize("name", READS)
def test_read_methods_do_not_require_confirm(name):
    assert odoo._op_writes(name) is False, (
        f"{name!r} is a read but is gated behind --confirm"
    )


def test_bare_verb_and_verb_object_are_both_covered():
    """The rule that broke twice: bare ``assign`` vs ``assigned_to``."""
    assert odoo._op_writes("assign") is True
    assert odoo._op_writes("assign_task") is True
    assert odoo._op_writes("assigned_to") is False
    assert odoo._op_writes("mark_sold") is True
    assert odoo._op_writes("marketplace_summary") is False


#: Frozen inventory of every method the CLI exposes that mutates data.
#:
#: A substring heuristic cannot police this. The previous version of the sweep
#: guessed at "mutating" names and so missed `research_comps` (calls the eBay
#: Browse API and writes recomputed comp aggregates back to the product) and
#: `learn_location` (persists an alias to location_vocab.json) — both of which
#: read like queries and ran ungated.
#:
#: So the expected set is written down instead of inferred. Adding any method
#: to an ops class fails the test below until it is classified here, which is
#: the point: the unsafe default is "read", and that default must never be
#: reached by accident.
EXPECTED_WRITES = {
    "add_attachment",
    "add_checklist_item",
    "add_component",
    "add_image",
    "add_item",
    "add_line",
    "add_part",
    "add_product",
    "apply_suggested_price",
    "approve_bid",
    "approve_leave",
    "assign",
    "assign_task",
    "cancel_order",
    "cancel_po",
    "cancel_task",
    "close",
    "close_session",
    "complete_task",
    "confirm_order",
    "confirm_po",
    "confirm_receipt",
    "create",
    "create_build",
    "create_build_order",
    "create_claim",
    "create_customer",
    "create_department",
    "create_employee",
    "create_event",
    "create_expense",
    "create_invoice",
    "create_lead",
    "create_leave_request",
    "create_listing",
    "create_opportunity",
    "create_order",
    "create_project",
    "create_purchase_order",
    "create_quotation",
    "create_registration",
    "create_repair",
    "create_rma",
    "create_session",
    "create_shipment",
    "create_task",
    "create_ticket",
    "delete_attachment",
    "delete_customer",
    "delete_event",
    "draft_ai_reply",
    "draft_reply",
    "end_listing",
    "find_or_create_partner",
    "find_or_create_product",
    "flag_exception",
    "generate_content",
    "learn_location",
    "log_oem_shipment",
    "log_time",
    "log_timesheet",
    "mark_listed",
    "mark_lost",
    "mark_read",
    "mark_renewed",
    "mark_sold",
    "mark_spam",
    "mark_won",
    "move_stage",
    "note_line",
    "post_customer_update",
    "post_invoice",
    "publish",
    "receive_line",
    "receive_oem_shipment",
    "receive_products",
    "record_tracking",
    "remove_component",
    "reopen",
    "reply",
    "reschedule",
    "rescrape",
    "research_comps",
    "reset_draft",
    "reset_task",
    "revise_draft",
    "revoke_approval",
    "run_action",
    "run_claim_action",
    "run_item_action",
    "run_product_action",
    "save_as_catalog",
    "schedule",
    "schedule_job",
    "schedule_pickup",
    "send_reply",
    "set_check",
    "set_diagnosis",
    "set_dismissed",
    "set_estimates",
    "set_line_resolution",
    "set_notes",
    "set_price",
    "set_pricing",
    "set_task_stage",
    "set_watching",
    "smart_create_employee",
    "smart_create_event",
    "smart_create_invoice",
    "smart_create_lead",
    "smart_create_purchase",
    "smart_create_quotation",
    "smart_create_task",
    "smart_create_todo",
    "start_task",
    "submit_expense",
    "toggle_checklist_item",
    "unschedule_job",
    "update",
    "update_customer",
    "update_employee",
    "update_event",
    "update_task",
}


def _all_exposed_methods():
    """Every public callable reachable as `<namespace>.<method>` from the CLI."""
    smart = SmartActionHandler(OdooClient(config=OdooConfig(
        url="https://test.odoo.com", db="d", username="u", api_key="k",
    )))
    out = set()
    for ns, attr in sorted(odoo.OPS_NAMESPACES.items()):
        obj = smart if attr is None else getattr(smart, attr)
        for name in sorted(dir(obj)):
            if name.startswith("_") or not callable(getattr(obj, name, None)):
                continue
            out.add(name)
    return out


def test_write_classification_matches_the_frozen_inventory():
    """Every exposed method is classified exactly as recorded — no drift.

    Fails in both directions on purpose:

    * a method newly classified as a write but absent from EXPECTED_WRITES
      (usually fine — add it), and
    * a method in EXPECTED_WRITES that no longer classifies as a write, which
      means a mutation slipped through the gate.
    """
    exposed = _all_exposed_methods()
    actual = {m for m in exposed if odoo._op_writes(m)}

    newly_gated = sorted(actual - EXPECTED_WRITES)
    no_longer_gated = sorted(EXPECTED_WRITES & exposed - actual)

    assert not no_longer_gated, (
        "these mutating methods are NO LONGER gated behind --confirm: "
        + ", ".join(no_longer_gated)
    )
    assert not newly_gated, (
        "these methods are now gated but are not in EXPECTED_WRITES — if they "
        "mutate, add them; if they are reads, the classifier over-matched: "
        + ", ".join(newly_gated)
    )


def test_inventory_has_no_stale_entries():
    """EXPECTED_WRITES must not name methods that no longer exist."""
    stale = sorted(EXPECTED_WRITES - _all_exposed_methods())
    assert not stale, (
        "EXPECTED_WRITES names method(s) no longer exposed: " + ", ".join(stale)
    )


def test_known_side_effecting_reads_are_gated():
    """Methods that look like queries but mutate — the ones that got missed."""
    for name in ("research_comps", "learn_location"):
        assert name in EXPECTED_WRITES, f"{name} must be inventoried as a write"
        assert odoo._op_writes(name) is True, (
            f"{name} mutates despite its name and must require --confirm"
        )


def test_base_ops_write_methods_are_gated_on_every_namespace():
    """``create``/``update`` are inherited everywhere — gate them everywhere."""
    for name in ("create", "update"):
        assert odoo._op_writes(name) is True, (
            f"BaseOps.{name} is inherited by every namespace and mutates"
        )
