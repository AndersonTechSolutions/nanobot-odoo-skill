"""Live check: every declared field must actually exist on its Odoo model.

Skipped unless real credentials are in the environment, so the offline suite
is unaffected:

    source ~/.config/odoo-toolbox/staging.env
    ODOO_USERNAME=$ODOO_USER ODOO_API_KEY=$ODOO_PASSWORD python3 -m pytest tests/test_live_fields.py -v

Why this exists as a test rather than a one-off script: ``read()`` rejects an
unknown field outright instead of skipping it, so a single bad name in
``DETAIL_FIELDS`` makes *every* ``get()`` on that namespace raise — and nothing
else notices. The list/search/summary methods use ``LIST_FIELDS`` and keep
working, so a smoke test that only calls those (as ours did) passes while
``get()`` is completely broken.

That is not hypothetical. This check found two live breakages on first run:

* ``OrderStatusOps`` declared ``partner_email`` / ``partner_phone`` on
  ``sale.order``, which has neither — contact details live on the partner.
* ``FieldServiceOps`` declared ``partner_email`` and ``kanban_state`` on
  ``project.task``, which has ``partner_phone``, ``state`` and ``stage_id``
  but neither of those. That one predated this branch, so
  ``field_service.get()`` had been raising since the ops class was written.

Mock-based tests cannot catch this class of bug by construction: the mock
returns whatever the test hands it, so a field name that does not exist on the
real model looks identical to one that does.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill import models as M  # noqa: E402
from odoo_skill.client import OdooClient  # noqa: E402
from odoo_skill.models._base import BaseOps  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (os.environ.get("ODOO_URL") and os.environ.get("ODOO_API_KEY")),
    reason="live Odoo credentials not in the environment",
)

#: Every ops class that declares field lists against a concrete model.
#:
#: TodoMatrixOps is deliberately absent: it predates BaseOps, is not a subclass,
#: and carries TASK_MODEL/CHECKLIST_MODEL/... rather than a single MODEL with
#: LIST_FIELDS/DETAIL_FIELDS. It has no field-list contract for this to check.
OPS_CLASSES = [
    M.FbMarketplaceOps, M.InboundOps, M.OrderStatusOps, M.EbayMessageOps,
    M.AuctionOps, M.PhotographyOps, M.PcBuildOps,
    M.RepairOps, M.HelpdeskOps, M.RMAOps, M.WarrantyOps, M.ConsignmentOps,
    M.MessagingOps, M.FieldServiceOps, M.EbayListingOps, M.ProductGuiOps,
    M.ITADOps,
]

assert all(issubclass(c, BaseOps) for c in OPS_CLASSES), (
    "OPS_CLASSES must contain only BaseOps subclasses — a class without the "
    "MODEL/LIST_FIELDS contract cannot be checked here"
)


@pytest.fixture(scope="module")
def live_client():
    client = OdooClient.from_env()
    client.authenticate()
    return client


@pytest.mark.parametrize("ops_class", OPS_CLASSES, ids=lambda c: c.__name__)
def test_effective_fields_all_exist_on_the_model(ops_class, live_client):
    """The list actually sent to Odoo must contain only real fields.

    Asserts on the *effective* list from ``BaseOps._fields()``, not the raw
    declarations. Declaring a field from an optional module is legitimate —
    ``helpdesk_repair`` adds ``repair.order.ticket_id`` and
    ``helpdesk.ticket.repair_ids``; ``atech_messaging`` adds
    ``project.task.sms_fsm_*`` — and those are present on staging but not on
    production. What must never happen is one of those names reaching ``read()``
    on a database that lacks it, because Odoo rejects the whole call.
    """
    ops = ops_class(live_client)
    if not ops.MODEL:
        pytest.skip(f"{ops_class.__name__} declares no MODEL")
    if not ops.available():
        pytest.skip(f"{ops.MODULE or ops.MODEL} not installed on this database")

    try:
        real = set(live_client.fields_get(ops.MODEL, attributes=["type"]).keys())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot introspect {ops.MODEL}: {str(exc)[:80]}")

    for detail in (False, True):
        effective = ops._fields(detail=detail)
        bad = sorted(f for f in effective if f not in real)
        assert not bad, (
            f"{ops_class.__name__}._fields(detail={detail}) would send field(s) "
            f"absent from {ops.MODEL}: {bad}"
        )
        assert effective, (
            f"{ops_class.__name__}._fields(detail={detail}) filtered down to "
            f"nothing — the declared list matches no real field"
        )


@pytest.mark.parametrize("ops_class", OPS_CLASSES, ids=lambda c: c.__name__)
def test_get_succeeds_on_a_real_record(ops_class, live_client):
    """The end-to-end proof: read one real record through DETAIL_FIELDS."""
    ops = ops_class(live_client)
    if not ops.MODEL:
        pytest.skip(f"{ops_class.__name__} declares no MODEL")
    if not ops.available():
        pytest.skip(f"{ops.MODULE or ops.MODEL} not installed on this database")

    try:
        rows = live_client.search_read(ops.MODEL, [], fields=["id"], limit=1)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no access to {ops.MODEL}: {str(exc)[:80]}")
    if not rows:
        pytest.skip(f"no {ops.MODEL} records on this database")

    record = ops.get(rows[0]["id"])
    assert record["id"] == rows[0]["id"]
