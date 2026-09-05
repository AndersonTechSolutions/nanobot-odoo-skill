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

The sweep at the end compares every exposed method against a frozen,
namespace-qualified inventory in ``tests/method_inventory.json``. BOTH reads
and writes are frozen: freezing only the writes lets a new method sit in
neither set and default to "read" without failing anything, which is how
``research_comps`` and ``learn_location`` shipped ungated.
"""

import json
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
    "set_watching", "set_dismissed", "revise", "revise_stage", "revise_discard",
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
    "revision_status",
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


#: Frozen inventory, namespace-qualified, in tests/method_inventory.json.
INVENTORY_PATH = os.path.join(os.path.dirname(__file__), "method_inventory.json")


def _load_inventory():
    with open(INVENTORY_PATH) as fh:
        data = json.load(fh)
    return set(data["writes"]), set(data["reads"])


def _all_exposed_methods():
    """Every public callable reachable as ``<namespace>.<method>`` from the CLI."""
    smart = SmartActionHandler(OdooClient(config=OdooConfig(
        url="https://test.odoo.com", db="d", username="u", api_key="k",
    )))
    out = set()
    for ns, attr in sorted(odoo.OPS_NAMESPACES.items()):
        obj = smart if attr is None else getattr(smart, attr)
        for name in sorted(dir(obj)):
            if name.startswith("_") or not callable(getattr(obj, name, None)):
                continue
            out.add(f"{ns}.{name}")
    return out


def test_no_method_is_unclassified():
    """Every exposed method must appear in the inventory — reads included.

    This is the assertion that matters, and the reason BOTH lists are frozen.
    An earlier version froze only the writes: a newly added method then sat in
    neither set, tripped no assertion, and defaulted to "read" — silently
    ungated. That is precisely how ``research_comps`` (writes recomputed comp
    aggregates) and ``learn_location`` (persists an alias file) shipped
    unconfirmed. Freezing the reads too makes "I forgot to classify it" a test
    failure instead of a silent grant.
    """
    writes, reads = _load_inventory()
    unclassified = sorted(_all_exposed_methods() - writes - reads)
    assert not unclassified, (
        "these methods are in neither list in tests/method_inventory.json, so "
        "they default to 'read' and run without --confirm. Classify each one: "
        + ", ".join(unclassified)
    )


def test_classification_matches_the_inventory():
    """The classifier must agree with the inventory, in both directions."""
    writes, reads = _load_inventory()
    exposed = _all_exposed_methods()

    should_write_but_does_not = sorted(
        q for q in writes & exposed if not odoo._op_writes(q.split(".", 1)[1])
    )
    should_read_but_gated = sorted(
        q for q in reads & exposed if odoo._op_writes(q.split(".", 1)[1])
    )

    assert not should_write_but_does_not, (
        "these mutating methods are NO LONGER gated behind --confirm: "
        + ", ".join(should_write_but_does_not)
    )
    assert not should_read_but_gated, (
        "these reads are now gated behind --confirm: "
        + ", ".join(should_read_but_gated)
    )


def test_inventory_has_no_stale_entries():
    """The inventory must not name methods that no longer exist."""
    writes, reads = _load_inventory()
    stale = sorted((writes | reads) - _all_exposed_methods())
    assert not stale, (
        "tests/method_inventory.json names method(s) no longer exposed: "
        + ", ".join(stale)
    )


def test_known_side_effecting_reads_are_gated():
    """Methods that look like queries but mutate — the ones that got missed."""
    writes, _ = _load_inventory()
    for qualified in ("ebay.research_comps", "smart.learn_location"):
        assert qualified in writes, f"{qualified} must be inventoried as a write"
        assert odoo._op_writes(qualified.split(".", 1)[1]) is True, (
            f"{qualified} mutates despite its name and must require --confirm"
        )


def test_base_ops_write_methods_are_gated_on_every_namespace():
    """``create``/``update`` are inherited everywhere — gate them everywhere."""
    for name in ("create", "update"):
        assert odoo._op_writes(name) is True, (
            f"BaseOps.{name} is inherited by every namespace and mutates"
        )
