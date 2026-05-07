"""Shared pytest fixtures for the Odoo skill test suite."""

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

# Ensure odoo_skill package is importable from the skill root
SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.config import OdooConfig
from odoo_skill.client import OdooClient
from odoo_skill.smart_actions import SmartActionHandler


@pytest.fixture()
def odoo_config():
    """Return a valid OdooConfig with test values."""
    return OdooConfig(
        url="https://test.odoo.com",
        db="test_db",
        username="admin@test.com",
        api_key="test-api-key-1234",
        timeout=30,
        max_retries=2,
        poll_interval=60,
        log_level="WARNING",
        webhook_port=8080,
        webhook_secret="webhook-secret",
    )


@pytest.fixture()
def mock_client(odoo_config):
    """Return an OdooClient with mocked XML-RPC proxies.

    ``_common`` and ``_models`` are MagicMock instances so no real
    network calls are made.  ``_uid`` is pre-set to ``2`` to skip
    lazy authentication in most tests.
    """
    client = OdooClient(config=odoo_config)
    client._common = MagicMock(name="common_proxy")
    client._models = MagicMock(name="models_proxy")
    client._uid = 2
    return client


@pytest.fixture()
def smart(mock_client):
    """Return a SmartActionHandler wrapping the mock client."""
    return SmartActionHandler(mock_client)
