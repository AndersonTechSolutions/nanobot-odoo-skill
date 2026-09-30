"""Tests for ``sales.set_ltl_freight`` (atech_ltl_freight server action).

The agent must never hand-price LTL freight: it hands pallets + carrier cost
to the server, which builds the single FRT-LTL line with palletization baked in.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.models.sale_order import SaleOrderOps  # noqa: E402


def _ops(result=None):
    client = MagicMock()
    client.execute.return_value = result or {
        "pallets": 2, "rate": 50.0, "palletization": 100.0,
        "carrier_cost": 885.65, "freight_price": 985.65, "line_id": 7,
    }
    return SaleOrderOps(client), client


def test_calls_server_action_with_exact_args():
    ops, client = _ops()
    res = ops.set_ltl_freight(1754, 2, 885.65)
    client.execute.assert_called_once_with(
        "sale.order", "action_set_ltl_freight", [1754], 2, 885.65)
    assert res["freight_price"] == 985.65


def test_never_passes_none_or_description():
    ops, client = _ops()
    ops.set_ltl_freight(1, "3", "100")
    args = client.execute.call_args.args
    assert args == ("sale.order", "action_set_ltl_freight", [1], 3, 100.0)
    assert None not in args


@pytest.mark.parametrize("pallets", [0, -1])
def test_rejects_non_positive_pallets(pallets):
    ops, client = _ops()
    with pytest.raises(ValueError):
        ops.set_ltl_freight(1, pallets, 100)
    client.execute.assert_not_called()


def test_rejects_negative_carrier_cost():
    ops, client = _ops()
    with pytest.raises(ValueError):
        ops.set_ltl_freight(1, 2, -5)
    client.execute.assert_not_called()
