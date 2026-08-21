"""Tests for odoo_skill.client -- OdooClient CRUD, auth, and error handling."""

import os
import xmlrpc.client
import pytest
from unittest.mock import MagicMock, patch

from odoo_skill.client import OdooClient
from odoo_skill.config import OdooConfig
from odoo_skill.errors import (
    OdooAuthenticationError,
    OdooAccessError,
    OdooConnectionError,
    OdooError,
    OdooRecordNotFoundError,
    OdooValidationError,
    classify_error,
)


# ── Authentication ────────────────────────────────────────────────


class TestAuthentication:
    """Test the authenticate() path -- success, failure, lazy uid."""

    def test_authenticate_success(self, mock_client):
        mock_client._uid = None  # reset so authenticate() actually runs
        mock_client._common.authenticate.return_value = 7
        uid = mock_client.authenticate()
        assert uid == 7
        assert mock_client._uid == 7
        mock_client._common.authenticate.assert_called_once_with(
            "test_db", "admin@test.com", "test-api-key-1234", {},
        )

    def test_authenticate_failure_returns_false(self, mock_client):
        mock_client._uid = None
        mock_client._common.authenticate.return_value = False
        with pytest.raises(OdooAuthenticationError, match="Authentication failed"):
            mock_client.authenticate()

    def test_authenticate_fault_raises(self, mock_client):
        mock_client._uid = None
        fault = xmlrpc.client.Fault(1, "AccessDenied: bad credentials")
        mock_client._common.authenticate.side_effect = fault
        with pytest.raises(OdooAuthenticationError):
            mock_client.authenticate()

    def test_lazy_uid_triggers_authenticate(self, mock_client):
        mock_client._uid = None
        mock_client._common.authenticate.return_value = 42
        uid = mock_client.uid
        assert uid == 42
        mock_client._common.authenticate.assert_called()

    def test_lazy_uid_cached(self, mock_client):
        """If _uid is already set, accessing .uid should NOT re-authenticate."""
        assert mock_client.uid == 2
        mock_client._common.authenticate.assert_not_called()


# ── CRUD convenience wrappers ─────────────────────────────────────


class TestCRUD:
    """Test search_read, search, read, create, write, unlink wrappers."""

    def test_search_read(self, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 1, "name": "Alice"},
        ]
        result = mock_client.search_read(
            "res.partner", [["is_company", "=", True]],
            fields=["name"], limit=5,
        )
        assert result == [{"id": 1, "name": "Alice"}]
        mock_client._models.execute_kw.assert_called_once_with(
            "test_db", 2, "test-api-key-1234",
            "res.partner", "search_read",
            [[["is_company", "=", True]]],
            {"limit": 5, "fields": ["name"]},
        )

    def test_search_read_default_limit(self, mock_client):
        mock_client._models.execute_kw.return_value = []
        mock_client.search_read("res.partner")
        call_kwargs = mock_client._models.execute_kw.call_args[0][6]
        assert call_kwargs["limit"] == 100

    def test_search(self, mock_client):
        mock_client._models.execute_kw.return_value = [1, 2, 3]
        result = mock_client.search("res.partner", [["active", "=", True]], limit=10)
        assert result == [1, 2, 3]

    def test_read_single_id_wrapped_in_list(self, mock_client):
        mock_client._models.execute_kw.return_value = [{"id": 5, "name": "Bob"}]
        result = mock_client.read("res.partner", 5, fields=["name"])
        assert result == [{"id": 5, "name": "Bob"}]
        # The single int should have been wrapped: args list is [[5]]
        call_args = mock_client._models.execute_kw.call_args[0][5]
        assert call_args == [[5]]

    def test_read_list_of_ids(self, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 5, "name": "Bob"},
            {"id": 6, "name": "Eve"},
        ]
        result = mock_client.read("res.partner", [5, 6], fields=["name"])
        assert len(result) == 2

    def test_create(self, mock_client):
        mock_client._models.execute_kw.return_value = 99
        new_id = mock_client.create("res.partner", {"name": "New"})
        assert new_id == 99

    def test_write_single_id(self, mock_client):
        mock_client._models.execute_kw.return_value = True
        ok = mock_client.write("res.partner", 5, {"name": "Updated"})
        assert ok is True
        call_args_list = mock_client._models.execute_kw.call_args[0][5]
        assert call_args_list == [[5], {"name": "Updated"}]

    def test_write_list_of_ids(self, mock_client):
        mock_client._models.execute_kw.return_value = True
        ok = mock_client.write("res.partner", [5, 6], {"name": "Updated"})
        assert ok is True

    def test_unlink(self, mock_client):
        mock_client._models.execute_kw.return_value = True
        ok = mock_client.unlink("res.partner", 5)
        assert ok is True

    def test_unlink_list(self, mock_client):
        mock_client._models.execute_kw.return_value = True
        ok = mock_client.unlink("res.partner", [5, 6])
        assert ok is True

    def test_search_count(self, mock_client):
        mock_client._models.execute_kw.return_value = 42
        count = mock_client.search_count("res.partner", [["active", "=", True]])
        assert count == 42

    def test_fields_get_caches_result(self, mock_client):
        mock_client._models.execute_kw.return_value = {
            "name": {"string": "Name", "type": "char"},
        }
        result1 = mock_client.fields_get("res.partner")
        result2 = mock_client.fields_get("res.partner")
        assert result1 is result2
        # Only one RPC call should have been made
        assert mock_client._models.execute_kw.call_count == 1

    def test_execute_fault_classified(self, mock_client):
        fault = xmlrpc.client.Fault(2, "AccessDenied: bad creds")
        mock_client._models.execute_kw.side_effect = fault
        with pytest.raises(OdooAuthenticationError):
            mock_client.execute("res.partner", "read", [1])


# ── Error classification ─────────────────────────────────────────


class TestErrorClassification:
    """Test that classify_error maps faults to the correct exception type."""

    def test_access_denied(self):
        fault = xmlrpc.client.Fault(1, "AccessDenied: bad creds")
        err = classify_error(fault, model="res.partner", method="search_read")
        assert isinstance(err, OdooAuthenticationError)

    def test_access_error(self):
        fault = xmlrpc.client.Fault(2, "AccessError: no permission")
        err = classify_error(fault, model="res.partner", method="write")
        assert isinstance(err, OdooAccessError)

    def test_validation_error(self):
        fault = xmlrpc.client.Fault(1, "ValidationError: field required")
        err = classify_error(fault, model="res.partner", method="create")
        assert isinstance(err, OdooValidationError)

    def test_user_error_maps_to_validation(self):
        fault = xmlrpc.client.Fault(1, "UserError: something wrong")
        err = classify_error(fault, model="sale.order", method="create")
        assert isinstance(err, OdooValidationError)

    def test_missing_error(self):
        fault = xmlrpc.client.Fault(1, "MissingError: record deleted")
        err = classify_error(fault, model="res.partner", method="read")
        assert isinstance(err, OdooRecordNotFoundError)

    def test_connection_error(self):
        exc = ConnectionError("refused")
        err = classify_error(exc, model="res.partner", method="search")
        assert isinstance(err, OdooConnectionError)

    def test_timeout_error(self):
        exc = TimeoutError("timed out")
        err = classify_error(exc, model="res.partner", method="search")
        assert isinstance(err, OdooConnectionError)

    def test_protocol_error(self):
        exc = xmlrpc.client.ProtocolError("https://x.com/xmlrpc", 503, "Unavailable", {})
        err = classify_error(exc, model="res.partner", method="search")
        assert isinstance(err, OdooConnectionError)

    def test_generic_fault(self):
        fault = xmlrpc.client.Fault(999, "SomeOtherError: oops")
        err = classify_error(fault, model="res.partner", method="search")
        assert isinstance(err, OdooError)
        assert not isinstance(err, OdooAuthenticationError)
        assert not isinstance(err, OdooAccessError)

    def test_error_preserves_model_and_method(self):
        fault = xmlrpc.client.Fault(1, "AccessDenied: x")
        err = classify_error(fault, model="sale.order", method="confirm")
        assert err.model == "sale.order"
        assert err.method == "confirm"


# ── Factory methods ───────────────────────────────────────────────

_CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if not k.startswith("ODOO_")
}


class TestFactoryMethods:
    """Test OdooClient.from_env and from_values."""

    def test_from_env(self, tmp_path):
        env = {
            **_CLEAN_ENV,
            "ODOO_URL": "https://factory.odoo.com",
            "ODOO_DB": "factory_db",
            "ODOO_USERNAME": "admin",
            "ODOO_API_KEY": "key123",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "odoo_skill.config._DEFAULT_CONFIG_PATH",
                tmp_path / "nope.json",
            ):
                client = OdooClient.from_env()
        assert client.config.url == "https://factory.odoo.com"
        assert client.config.db == "factory_db"

    def test_from_values(self):
        client = OdooClient.from_values(
            url="https://vals.odoo.com",
            db="v_db",
            username="v_user",
            api_key="v_key",
        )
        assert client.config.url == "https://vals.odoo.com"
        assert client.config.db == "v_db"

    def test_from_values_strips_trailing_slash(self):
        client = OdooClient.from_values(
            url="https://vals.odoo.com/",
            db="v_db",
            username="v_user",
            api_key="v_key",
        )
        assert client.config.url == "https://vals.odoo.com"

    def test_from_values_invalid_raises(self):
        with pytest.raises(ValueError):
            OdooClient.from_values(url="", db="", username="", api_key="")


# ── Diagnostics ───────────────────────────────────────────────────


class TestDiagnostics:
    """Test version() and test_connection()."""

    def test_version(self, mock_client):
        mock_client._common.version.return_value = {"server_version": "17.0"}
        ver = mock_client.version()
        assert ver["server_version"] == "17.0"

    def test_test_connection_success(self, mock_client):
        mock_client._common.version.return_value = {"server_version": "17.0"}
        info = mock_client.test_connection()
        assert info["status"] == "connected"
        assert info["uid"] == 2
        assert info["server_version"] == "17.0"

    def test_test_connection_failure(self, mock_client):
        mock_client._common.version.side_effect = ConnectionError("down")
        info = mock_client.test_connection()
        assert info["status"] == "error"
        assert "down" in info["message"]


# ── Null-return marshalling ──────────────────────────────────────────


class TestNullMarshalFault:
    """Odoo cannot encode a ``None`` return over XML-RPC.

    Its controller calls ``xmlrpc.client.dumps(..., allow_none=False)``, which
    is hardcoded server-side, so a button method ending without a ``return``
    produces a Fault *after* the call has run and committed. Verified live: the
    record shows the write applied. Raising it would make a caller retry an
    action that already happened, so ``execute`` maps exactly this fault to
    ``None`` — and nothing else.
    """

    def _fault(self, message):
        import xmlrpc.client
        return xmlrpc.client.Fault(1, message)

    def test_null_marshal_fault_becomes_none(self, mock_client):
        mock_client._models.execute_kw.side_effect = self._fault(
            "Traceback (most recent call last):\n  ...\n"
            "TypeError: cannot marshal None unless allow_none is enabled\n"
        )
        assert mock_client.execute("repair.order", "action_repair_start", [1]) is None

    def test_other_faults_still_raise(self, mock_client):
        from odoo_skill.errors import OdooError
        mock_client._models.execute_kw.side_effect = self._fault(
            "Paste the Facebook Marketplace URL before marking as listed."
        )
        with pytest.raises(OdooError):
            mock_client.execute("fb.marketplace.listing", "action_mark_listed", [1])

    def test_access_faults_still_raise(self, mock_client):
        from odoo_skill.errors import OdooError
        mock_client._models.execute_kw.side_effect = self._fault(
            "You are not allowed to access 'Photo Session' (photo.session) records."
        )
        with pytest.raises(OdooError):
            mock_client.execute("photo.session", "search_read", [])

    def test_run_action_reports_the_post_action_record(self, mock_client):
        """A None return must still yield the record's real post-action state."""
        from odoo_skill.models.fb_marketplace import FbMarketplaceOps
        ops = FbMarketplaceOps(mock_client)
        ops._available = True
        ops._model_field_cache = set()
        mock_client._models.execute_kw.side_effect = [
            self._fault("TypeError: cannot marshal None unless allow_none is enabled"),
            [{"id": 7, "name": "Dell", "state": "sold"}],
        ]
        result = ops.run_action(7, "action_mark_sold")
        assert result["returned"] is None
        assert result["record"]["state"] == "sold"
