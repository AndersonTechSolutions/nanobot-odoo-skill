"""Tests for odoo_skill.smart_actions -- SmartActionHandler."""

import pytest
from unittest.mock import MagicMock, patch, call


# ── find_or_create_partner ────────────────────────────────────────


class TestFindOrCreatePartner:
    """Test partner lookup and creation logic."""

    def test_finds_existing_partner(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 10, "name": "Acme Corp", "email": "a@acme.com",
             "phone": "555", "is_company": True, "customer_rank": 1,
             "supplier_rank": 0},
        ]
        result = smart.find_or_create_partner("Acme Corp")
        assert result["created"] is False
        assert result["partner"]["id"] == 10
        assert result["partner"]["name"] == "Acme Corp"

    def test_creates_partner_when_not_found(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [
            [],   # search_read finds nothing
            20,   # create returns new id
            [{"id": 20, "name": "NewCo", "email": "", "phone": "",
              "is_company": True}],  # read
        ]
        result = smart.find_or_create_partner("NewCo", allow_create=True)
        assert result["created"] is True
        assert result["partner"]["id"] == 20
        assert result["matched"] == []

    def test_prefers_exact_name_match(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 1, "name": "Acme Inc", "email": "", "phone": "",
             "is_company": True, "customer_rank": 1, "supplier_rank": 0},
            {"id": 2, "name": "acme", "email": "", "phone": "",
             "is_company": True, "customer_rank": 1, "supplier_rank": 0},
        ]
        # Searching for "acme" should prefer the exact match (id=2)
        result = smart.find_or_create_partner("acme")
        assert result["partner"]["id"] == 2

    def test_returns_first_when_no_exact_match(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 1, "name": "Acme Industries", "email": "", "phone": "",
             "is_company": True, "customer_rank": 1, "supplier_rank": 0},
            {"id": 2, "name": "Acme Corp", "email": "", "phone": "",
             "is_company": True, "customer_rank": 1, "supplier_rank": 0},
        ]
        result = smart.find_or_create_partner("acm")
        # No exact match, should return the first result
        assert result["partner"]["id"] == 1


# ── find_or_create_product ────────────────────────────────────────


class TestFindOrCreateProduct:
    """Test product lookup and creation logic."""

    def test_finds_existing_product(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 100, "name": "Widget", "default_code": "WDG",
             "list_price": 9.99, "type": "consu"},
        ]
        result = smart.find_or_create_product("Widget")
        assert result["created"] is False
        assert result["product"]["id"] == 100

    def test_creates_product_when_not_found(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [
            [],    # search_read
            200,   # create
            [{"id": 200, "name": "Gizmo", "default_code": False,
              "list_price": 0.0, "type": "consu"}],  # read
        ]
        result = smart.find_or_create_product("Gizmo", allow_create=True)
        assert result["created"] is True
        assert result["product"]["id"] == 200

    def test_created_product_uses_defaults(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [
            [],    # search_read
            201,   # create
            [{"id": 201, "name": "Thing", "default_code": False,
              "list_price": 15.0, "type": "consu"}],  # read
        ]
        result = smart.find_or_create_product("Thing", list_price=15.0, allow_create=True)
        assert result["created"] is True
        # Verify create was called with the list_price default
        create_call = mock_client._models.execute_kw.call_args_list[1]
        create_vals = create_call[0][5][0]  # args list -> first positional -> values dict
        assert create_vals["list_price"] == 15.0


# ── smart_create_quotation ────────────────────────────────────────


class TestSmartCreateQuotation:
    """Test the composite quotation workflow."""

    def test_creates_quotation_with_existing_entities(self, smart, mock_client):
        mock_client._models.execute_kw.side_effect = [
            # find_or_create_partner search_read (found)
            [{"id": 50, "name": "Rocky", "email": "", "phone": "",
              "is_company": True, "customer_rank": 1, "supplier_rank": 0}],
            # find_or_create_product search_read (found)
            [{"id": 60, "name": "Rock", "default_code": "RCK",
              "list_price": 5.0, "type": "consu"}],
        ]

        with patch.object(smart.sales, "create_quotation") as mock_cq:
            mock_cq.return_value = {"id": 70, "name": "S00001"}
            result = smart.smart_create_quotation(
                customer_name="Rocky",
                product_lines=[{"name": "Rock", "quantity": 5}],
            )

        assert result["order"]["name"] == "S00001"
        assert result["customer"]["created"] is False
        assert result["products"][0]["created"] is False
        assert "summary" in result

    def test_creates_quotation_with_new_customer_and_product(
        self, smart, mock_client,
    ):
        mock_client._models.execute_kw.side_effect = [
            [],   # partner search_read (not found)
            50,   # partner create
            [{"id": 50, "name": "Rocky", "email": "", "phone": "",
              "is_company": True}],  # partner read
            [],   # product search_read (not found)
            60,   # product create
            [{"id": 60, "name": "Rock", "default_code": False,
              "list_price": 0.0, "type": "consu"}],  # product read
        ]

        with patch.object(smart.sales, "create_quotation") as mock_cq:
            mock_cq.return_value = {"id": 71, "name": "S00002"}
            result = smart.smart_create_quotation(
                customer_name="Rocky",
                product_lines=[{"name": "Rock", "quantity": 5}],
                allow_create=True,
            )

        assert result["customer"]["created"] is True
        assert result["products"][0]["created"] is True
        assert result["order"]["id"] == 71

    def test_quotation_summary_contains_name(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 50, "name": "Rocky", "email": "", "phone": "",
             "is_company": True, "customer_rank": 1, "supplier_rank": 0},
        ]

        with patch.object(smart, "find_or_create_product") as mock_fop:
            mock_fop.return_value = {
                "product": {"id": 60, "name": "Rock"},
                "created": False, "matched": [],
            }
            with patch.object(smart.sales, "create_quotation") as mock_cq:
                mock_cq.return_value = {"id": 72, "name": "S00003"}
                result = smart.smart_create_quotation(
                    customer_name="Rocky",
                    product_lines=[{"name": "Rock", "quantity": 1}],
                )

        assert "S00003" in result["summary"]
        assert "Rocky" in result["summary"]


# ── smart_create_lead ─────────────────────────────────────────────


class TestSmartCreateLead:
    """Test CRM lead creation with optional partner linking."""

    def test_create_lead_without_contact(self, smart, mock_client):
        with patch.object(smart.crm, "create_lead") as mock_cl:
            mock_cl.return_value = {"id": 300, "name": "Hot lead"}
            result = smart.smart_create_lead(name="Hot lead")

        assert result["lead"]["id"] == 300
        assert result["partner"] is None

    def test_create_lead_with_contact_links_partner(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 11, "name": "Jane", "email": "j@co.com", "phone": "",
             "is_company": False, "customer_rank": 1, "supplier_rank": 0},
        ]

        with patch.object(smart.crm, "create_lead") as mock_cl:
            mock_cl.return_value = {"id": 301, "name": "Jane's deal"}
            result = smart.smart_create_lead(
                name="Jane's deal",
                contact_name="Jane",
                email="j@co.com",
            )

        assert result["partner"]["partner"]["id"] == 11
        # Verify partner_id was passed as a kwarg to create_lead
        _, kwargs = mock_cl.call_args
        assert kwargs["partner_id"] == 11


# ── smart_create_todo ─────────────────────────────────────────────


class TestSmartCreateTodo:
    """Test to-do task creation via the priority matrix."""

    def test_creates_todo_for_employee(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 5, "name": "Ian", "job_title": "Dev",
             "department_id": [1, "Engineering"]},
        ]

        with patch.object(smart.todo_matrix, "create_task") as mock_ct:
            mock_ct.return_value = {
                "id": 400,
                "name": "Review budget",
                "eisenhower_quadrant": "do",
            }
            result = smart.smart_create_todo(
                task_name="Review budget",
                employee_name="Ian",
                is_urgent=True,
                is_important=True,
            )

        assert result["task"]["id"] == 400
        assert result["quadrant"] == "do"
        assert result["employee"]["id"] == 5
        assert "Do First" in result["summary"]

    def test_schedule_quadrant(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 5, "name": "Ian", "job_title": "Dev",
             "department_id": [1, "Engineering"]},
        ]

        with patch.object(smart.todo_matrix, "create_task") as mock_ct:
            mock_ct.return_value = {
                "id": 401,
                "name": "Plan project",
                "eisenhower_quadrant": "schedule",
            }
            result = smart.smart_create_todo(
                task_name="Plan project",
                employee_name="Ian",
                is_urgent=False,
                is_important=True,
            )

        assert result["quadrant"] == "schedule"
        assert "Schedule" in result["summary"]

    def test_raises_when_employee_not_found(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = []
        with pytest.raises(ValueError, match="No employee found"):
            smart.smart_create_todo(
                task_name="Task",
                employee_name="Nobody",
            )


# ── smart_get_matrix ──────────────────────────────────────────────


class TestSmartGetMatrix:
    """Test Eisenhower matrix retrieval."""

    def test_returns_matrix_for_employee(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = [
            {"id": 5, "name": "Ian"},
        ]

        with patch.object(smart.todo_matrix, "get_matrix") as mock_gm:
            mock_gm.return_value = {
                "do": [{"id": 1}],
                "schedule": [],
                "delegate": [{"id": 2}],
                "eliminate": [],
                "summary": {
                    "do": 1, "schedule": 0, "delegate": 1,
                    "eliminate": 0, "total": 2,
                },
            }
            result = smart.smart_get_matrix(employee_name="Ian")

        assert result["matrix"]["summary"]["total"] == 2
        assert result["employee"]["name"] == "Ian"
        assert "Priority Matrix" in result["summary"]

    def test_raises_when_employee_not_found(self, smart, mock_client):
        mock_client._models.execute_kw.return_value = []
        with pytest.raises(ValueError, match="No employee found"):
            smart.smart_get_matrix(employee_name="Ghost")


# ── smart_get_team_workload ───────────────────────────────────────


class TestSmartGetTeamWorkload:
    """Test team workload retrieval."""

    def test_returns_workload_data(self, smart, mock_client):
        with patch.object(smart.todo_matrix, "get_team_workload") as mock_gw:
            mock_gw.return_value = {
                "team_totals": {
                    "employee_count": 3,
                    "total_active": 15,
                    "total_overdue": 2,
                    "total_estimated_hours": 40.5,
                },
                "employees": [],
            }
            result = smart.smart_get_team_workload()

        assert result["workload"]["team_totals"]["employee_count"] == 3
        assert "3 members" in result["summary"]
        assert "15 active" in result["summary"]
        assert "2 overdue" in result["summary"]
