"""Tests for odoo_skill.config -- OdooConfig validation and load_config."""

import json
import os
import pytest
from unittest.mock import patch

from odoo_skill.config import OdooConfig, load_config


# ── OdooConfig.validate ───────────────────────────────────────────


class TestOdooConfigValidation:
    """Validate that OdooConfig catches missing and malformed fields."""

    def test_valid_config_has_no_errors(self, odoo_config):
        assert odoo_config.validate() == []
        assert odoo_config.is_valid is True

    def test_missing_url(self):
        cfg = OdooConfig(db="db", username="u", api_key="k")
        errors = cfg.validate()
        assert any("ODOO_URL" in e for e in errors)

    def test_missing_db(self):
        cfg = OdooConfig(url="https://x.com", username="u", api_key="k")
        errors = cfg.validate()
        assert any("ODOO_DB" in e for e in errors)

    def test_missing_username(self):
        cfg = OdooConfig(url="https://x.com", db="db", api_key="k")
        errors = cfg.validate()
        assert any("ODOO_USERNAME" in e for e in errors)

    def test_missing_api_key(self):
        cfg = OdooConfig(url="https://x.com", db="db", username="u")
        errors = cfg.validate()
        assert any("ODOO_API_KEY" in e for e in errors)

    def test_bad_url_scheme(self):
        cfg = OdooConfig(url="ftp://bad.com", db="db", username="u", api_key="k")
        errors = cfg.validate()
        assert any("http://" in e for e in errors)

    def test_multiple_missing_fields(self):
        cfg = OdooConfig()
        errors = cfg.validate()
        # url, db, username, api_key are all required
        assert len(errors) >= 4
        assert cfg.is_valid is False

    def test_http_url_accepted(self):
        cfg = OdooConfig(url="http://local.dev", db="db", username="u", api_key="k")
        assert cfg.validate() == []

    def test_https_url_accepted(self):
        cfg = OdooConfig(url="https://prod.odoo.com", db="db", username="u", api_key="k")
        assert cfg.validate() == []

    def test_defaults_for_optional_fields(self):
        cfg = OdooConfig(url="https://x.com", db="db", username="u", api_key="k")
        assert cfg.timeout == 60
        assert cfg.max_retries == 3
        assert cfg.poll_interval == 60
        assert cfg.log_level == "INFO"

    def test_is_valid_property_reflects_validate(self):
        good = OdooConfig(url="https://x.com", db="d", username="u", api_key="k")
        bad = OdooConfig()
        assert good.is_valid is True
        assert bad.is_valid is False


# ── load_config ───────────────────────────────────────────────────


# Build a clean environment dict stripped of any real ODOO_* vars so
# tests are deterministic regardless of the developer's shell.
_CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if not k.startswith("ODOO_")
}


class TestLoadConfig:
    """Tests for loading config from env vars, files, and combinations."""

    def test_load_from_env_vars(self, tmp_path):
        env = {
            **_CLEAN_ENV,
            "ODOO_URL": "https://env.odoo.com",
            "ODOO_DB": "env_db",
            "ODOO_USERNAME": "env_user",
            "ODOO_API_KEY": "env_key",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(config_path=str(tmp_path / "nonexistent.json"))
        assert cfg.url == "https://env.odoo.com"
        assert cfg.db == "env_db"
        assert cfg.username == "env_user"
        assert cfg.api_key == "env_key"

    def test_load_from_file(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "url": "https://file.odoo.com",
            "db": "file_db",
            "username": "file_user",
            "api_key": "file_key",
            "timeout": 120,
        }))
        with patch.dict(os.environ, _CLEAN_ENV, clear=True):
            cfg = load_config(config_path=str(config_file))
        assert cfg.url == "https://file.odoo.com"
        assert cfg.db == "file_db"
        assert cfg.timeout == 120

    def test_env_overrides_file(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "url": "https://file.odoo.com",
            "db": "file_db",
            "username": "file_user",
            "api_key": "file_key",
        }))
        env = {
            **_CLEAN_ENV,
            "ODOO_URL": "https://env-wins.odoo.com",
            "ODOO_DB": "env_db",
            "ODOO_USERNAME": "env_user",
            "ODOO_API_KEY": "env_key",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(config_path=str(config_file))
        assert cfg.url == "https://env-wins.odoo.com"
        assert cfg.db == "env_db"

    def test_raises_on_invalid_config(self, tmp_path):
        with patch.dict(os.environ, _CLEAN_ENV, clear=True):
            with pytest.raises(ValueError, match="Odoo configuration is incomplete"):
                load_config(config_path=str(tmp_path / "missing.json"))

    def test_trailing_slash_stripped_from_url(self, tmp_path):
        env = {
            **_CLEAN_ENV,
            "ODOO_URL": "https://trail.odoo.com/",
            "ODOO_DB": "db",
            "ODOO_USERNAME": "u",
            "ODOO_API_KEY": "k",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(config_path=str(tmp_path / "nope.json"))
        assert cfg.url == "https://trail.odoo.com"

    def test_timeout_from_env_parsed_as_int(self, tmp_path):
        env = {
            **_CLEAN_ENV,
            "ODOO_URL": "https://x.com",
            "ODOO_DB": "db",
            "ODOO_USERNAME": "u",
            "ODOO_API_KEY": "k",
            "ODOO_TIMEOUT": "90",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(config_path=str(tmp_path / "nope.json"))
        assert cfg.timeout == 90
