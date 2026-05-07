"""Tests for odoo.py -- CLI argparse, _parse_lines, and command dispatch."""

import json
import sys
import os
import argparse
import pytest
from unittest.mock import MagicMock, patch

# Ensure the skill root is on sys.path so ``import odoo`` resolves to
# the CLI script rather than some other module.
SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import odoo as cli_module  # noqa: E402


# ── build_parser: subcommand arg parsing ──────────────────────────


class TestBuildParser:
    """Test that argparse subcommands are registered and parse correctly."""

    def test_all_subcommands_exist(self):
        parser = cli_module.build_parser()
        subcommands = [
            "create-quotation --customer X --lines []",
            "create-lead --name Test",
            "create-todo --task T --employee E",
            "get-matrix --employee E",
            "team-workload",
            "create-partner --name N",
            "find-partner --name N",
            "create-product --name P",
            "find-product --name P",
            "list-todos",
            "start-task --id 1",
            "complete-task --id 1",
            "cancel-task --id 1",
            "get-task --id 1",
            "update-task --id 1 --name X",
            "add-checklist --task-id 1 --name item",
            "toggle-checklist --id 1 --done",
            "get-checklist --task-id 1",
            "search-employees",
            "get-categories",
        ]
        for cmd_str in subcommands:
            args = parser.parse_args(cmd_str.split())
            assert args.command is not None

    def test_create_quotation_args(self):
        parser = cli_module.build_parser()
        args = parser.parse_args([
            "create-quotation", "--customer", "Acme",
            "--lines", '[{"name":"W","quantity":1}]',
            "--notes", "Test note",
        ])
        assert args.customer == "Acme"
        assert args.notes == "Test note"

    def test_create_todo_flags(self):
        parser = cli_module.build_parser()
        args = parser.parse_args([
            "create-todo", "--task", "Review", "--employee", "Ian",
            "--urgent", "--important", "--deadline", "2026-04-15",
        ])
        assert args.urgent is True
        assert args.important is True
        assert args.deadline == "2026-04-15"

    def test_list_todos_optional_args_default_none(self):
        parser = cli_module.build_parser()
        args = parser.parse_args(["list-todos"])
        assert args.employee is None
        assert args.state is None
        assert args.quadrant is None

    def test_get_matrix_requires_employee(self):
        parser = cli_module.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["get-matrix"])  # missing --employee

    def test_update_task_urgent_flags(self):
        parser = cli_module.build_parser()
        args_urgent = parser.parse_args(["update-task", "--id", "1", "--urgent"])
        assert args_urgent.urgent is True
        args_no_urgent = parser.parse_args(["update-task", "--id", "1", "--no-urgent"])
        assert args_no_urgent.urgent is False


# ── _parse_lines ──────────────────────────────────────────────────


class TestParseLines:
    """Test JSON line parsing for --lines arguments."""

    def test_valid_json_array(self):
        lines = cli_module._parse_lines('[{"name": "Widget", "quantity": 5}]')
        assert len(lines) == 1
        assert lines[0]["name"] == "Widget"

    def test_single_json_object_wrapped(self):
        lines = cli_module._parse_lines('{"name": "Widget"}')
        assert isinstance(lines, list)
        assert len(lines) == 1
        assert lines[0]["name"] == "Widget"

    def test_invalid_json_exits(self):
        with pytest.raises(SystemExit):
            cli_module._parse_lines("not json")

    def test_non_array_non_object_exits(self):
        with pytest.raises(SystemExit):
            cli_module._parse_lines('"just a string"')


# ── CLI dispatch with mocked SmartActionHandler ───────────────────


class TestCLIDispatch:
    """Test that cmd_* functions call the right SmartActionHandler methods."""

    def test_cmd_create_lead(self, capsys):
        mock_smart = MagicMock()
        mock_smart.smart_create_lead.return_value = {
            "lead": {"id": 1, "name": "Test"},
            "partner": None,
            "summary": "Lead created",
        }
        args = argparse.Namespace(
            name="Test Lead", contact=None, email=None,
            phone=None, expected_revenue=None,
        )
        with patch("odoo._get_smart", return_value=mock_smart):
            cli_module.cmd_create_lead(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["lead"]["id"] == 1

    def test_cmd_team_workload(self, capsys):
        mock_smart = MagicMock()
        mock_smart.smart_get_team_workload.return_value = {
            "workload": {},
            "summary": "Team: 2 members",
        }
        args = argparse.Namespace()
        with patch("odoo._get_smart", return_value=mock_smart):
            cli_module.cmd_team_workload(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "summary" in output

    def test_cmd_get_matrix(self, capsys):
        mock_smart = MagicMock()
        mock_smart.smart_get_matrix.return_value = {
            "matrix": {"do": [], "schedule": [], "delegate": [], "eliminate": []},
            "employee": {"id": 3, "name": "Ian"},
            "summary": "Matrix for Ian",
        }
        args = argparse.Namespace(employee="Ian")
        with patch("odoo._get_smart", return_value=mock_smart):
            cli_module.cmd_get_matrix(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["employee"]["name"] == "Ian"

    def test_cmd_create_todo(self, capsys):
        mock_smart = MagicMock()
        mock_smart.smart_create_todo.return_value = {
            "task": {"id": 10, "name": "Review"},
            "employee": {"id": 3, "name": "Ian"},
            "quadrant": "do",
            "summary": "Created",
        }
        args = argparse.Namespace(
            task="Review", employee="Ian", urgent=True,
            important=True, deadline="2026-04-15", description=None,
        )
        with patch("odoo._get_smart", return_value=mock_smart):
            cli_module.cmd_create_todo(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["task"]["id"] == 10
        mock_smart.smart_create_todo.assert_called_once_with(
            task_name="Review", employee_name="Ian",
            is_urgent=True, is_important=True,
            deadline="2026-04-15", description=None,
        )

    def test_cmd_complete_task(self, capsys):
        mock_smart = MagicMock()
        mock_smart.todo_matrix.complete_task.return_value = {
            "id": 5, "state": "done",
        }
        args = argparse.Namespace(id=5)
        with patch("odoo._get_smart", return_value=mock_smart):
            cli_module.cmd_complete_task(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["state"] == "done"

    def test_cmd_get_categories(self, capsys):
        mock_smart = MagicMock()
        mock_smart.todo_matrix.get_categories.return_value = [
            {"id": 1, "name": "Bug", "color": 1},
        ]
        args = argparse.Namespace()
        with patch("odoo._get_smart", return_value=mock_smart):
            cli_module.cmd_get_categories(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output[0]["name"] == "Bug"


# ── main() entry point ───────────────────────────────────────────


class TestMainFunction:
    """Test the main() entry point end-to-end with mocked _get_smart."""

    def test_no_args_exits_with_error(self):
        with patch.object(sys, "argv", ["odoo.py"]):
            with pytest.raises(SystemExit) as exc_info:
                cli_module.main()
        # argparse exits with code 2 when required subcommand is missing
        assert exc_info.value.code == 2
