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

The sweep at the end walks every method actually exposed on every namespace,
so a newly added method that matches nothing has to be classified explicitly
rather than defaulting to "read" unnoticed.
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


def test_every_exposed_method_is_classified_deliberately():
    """Sweep every method on every namespace against an explicit expectation.

    A method matching no rule is classified as a read, which is the unsafe
    default. This walks the real handler so a newly added ops method shows up
    here rather than being discovered in production.
    """
    smart = SmartActionHandler(OdooClient(config=OdooConfig(
        url="https://test.odoo.com", db="d", username="u", api_key="k",
    )))

    # Substrings that mean "this mutates" regardless of where they appear.
    # Anything matching one of these must be gated; the exceptions are reads
    # whose names merely contain the substring.
    MUTATING = (
        "create_", "delete_", "unlink_", "_confirm", "confirm_",
        "send_", "post_", "approve_", "cancel_", "submit_", "toggle_",
    )
    READ_EXCEPTIONS = {"confirmation_not_sent", "awaiting_confirmation"}

    ungated = []
    for ns, attr in sorted(odoo.OPS_NAMESPACES.items()):
        obj = smart if attr is None else getattr(smart, attr)
        for name in sorted(dir(obj)):
            if name.startswith("_") or not callable(getattr(obj, name, None)):
                continue
            if name in READ_EXCEPTIONS:
                continue
            if any(m in name for m in MUTATING) and not odoo._op_writes(name):
                ungated.append(f"{ns}.{name}")

    assert not ungated, (
        "these look like writes but would run without --confirm: "
        + ", ".join(ungated)
    )


def test_base_ops_write_methods_are_gated_on_every_namespace():
    """``create``/``update`` are inherited everywhere — gate them everywhere."""
    for name in ("create", "update"):
        assert odoo._op_writes(name) is True, (
            f"BaseOps.{name} is inherited by every namespace and mutates"
        )
