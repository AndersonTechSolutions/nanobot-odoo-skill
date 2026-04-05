---
name: odoo
description: Build or use the Odoo ERP connector for OpenClaw (Sales, CRM, Purchase, Inventory, Projects, HR, Fleet, Manufacturing integration via XML-RPC).
repository: https://github.com/AndersonTechSolutions/openclaw-odoo-skill
---

# Odoo ERP Connector

Full-featured Odoo 19 ERP integration for OpenClaw. Control your entire business via natural language chat commands.

**📦 Full Source Code:** https://github.com/AndersonTechSolutions/openclaw-odoo-skill

## Quick Install

\ash
npx clawhub install odoo-erp-connector
\

## Overview

The Odoo ERP Connector bridges OpenClaw and Odoo 19, enabling autonomous, chat-driven control over 153+ business modules including:
- Sales & CRM
- Purchasing & Inventory  
- Invoicing & Accounting
- Projects & Task Management
- Human Resources
- Fleet Management
- Manufacturing (MRP)
- Calendar & Events
- eCommerce

All operations use **smart actions** that handle fuzzy matching and auto-creation workflows.

## Capabilities

### Sales & CRM
- Create quotations with dynamic line items
- Manage sales orders (draft → confirmed → done)
- Search and filter orders by status, customer, date range
- Create and qualify leads and opportunities
- Move leads through CRM pipeline stages
- View full sales pipeline with revenue forecasting

### Purchasing
- Create purchase orders from vendors
- Manage PO status (draft → purchase → received)
- Receive and validate goods
- Search and filter POs by vendor, status, date
- Track purchase history and vendor performance

### Inventory & Products
- Create products (consumables, stockable, services)
- Query stock levels and availability
- Set reorder points and receive low-stock alerts
- Search products by name, code, or category
- Track stock movements and valuations

### Invoicing & Accounting
- Create and post customer invoices
- Manage payment terms and schedules
- Query unpaid and overdue invoices
- Search by customer, date range, or amount
- Track invoice status (draft → posted → paid)

### Projects & Tasks
- Create projects and organize by team/status
- Create tasks with priority, dates, and assignments
- Log timesheets and track project hours
- Search and filter tasks by project, status, assignee
- Manage project stages and closure

### Human Resources
- Create employees and departments
- Manage job titles and work schedules
- Process expense reports and reimbursements
- Search employees by name, department, job
- Track leave requests and attendance

### Fleet Management
- Create and track vehicles
- Log odometer readings and service records
- Track maintenance schedules and costs
- Search fleet by license plate, status, brand
- Generate fleet reports

### Manufacturing (MRP)
- Create Bills of Materials (BOMs)
- Manage manufacturing orders (MOs)
- Track component requirements and production status
- Search MOs by product or status
- Link BOMs to product variants

### Calendar & Events
- Create meetings and events with attendees
- Set reminders and locations
- Search events by date range or attendee
- Track calendar availability

### To-Do Priority Matrix (Eisenhower)
- Create to-do tasks with urgent/important flags
- View Eisenhower matrix per employee (Do/Schedule/Delegate/Eliminate)
- Get team workload dashboard with per-employee breakdowns
- Manage task states (todo → in progress → done)
- Manage subtask checklists per task
- Search and filter tasks by quadrant, state, or employee
- Track overdue tasks and estimated hours

### eCommerce
- Publish products to website
- View website orders and customer activity
- Manage product visibility and pricing

## Command Examples

### Sales
- "Create a quotation for Acme Corp with 10 Widgets at $50 each"
- "Confirm sales order SO00042"
- "Show me all draft quotations from the past week"
- "What's the total revenue from completed orders this month?"
- "Create a quote for Rocky with product Rock"

### CRM
- "Create a lead for Rocky, email rocky@example.com, potential $50k deal"
- "Move lead #47 to Qualified stage"
- "Show me the sales pipeline with all open opportunities"
- "What leads are at proposal stage?"
- "Create an opportunity for Acme with $100k expected value"

### Purchasing
- "Create a PO for 500 widgets from Supplier ABC"
- "Confirm purchase order PO00123"
- "Show all pending purchase orders"
- "Get me the vendor history for ABC Supplies"
- "What's on order that's overdue?"

### Inventory & Products
- "Create a new product: TestWidget, $25 price, min stock 10"
- "Show products with stock below 20 units"
- "What's the stock level for Widget X?"
- "Search for all consumable products"
- "Set reorder point for Product Y to 50 units"

### Invoicing
- "Create an invoice for Acme Corp with 5 units at $50 each"
- "Show me unpaid invoices"
- "What invoices are overdue?"
- "Post invoice INV-001"
- "Send a reminder for invoice INV-002"

### Projects & Tasks
- "Create a project called Website Redesign"
- "Create a task 'Fix login button' in Website Redesign project"
- "Show me all tasks assigned to me"
- "Log 3 hours of work on task #42"
- "What's the status of the Website Redesign project?"

### HR
- "Create employee John Smith, job title Developer"
- "Create department Engineering"
- "Show me all employees in Engineering"
- "Submit expense report for $45.99"
- "What are the pending leave requests?"

### Fleet
- "Create vehicle: Tesla Model 3, license plate TESLA-001"
- "Log odometer reading: 50,000 miles for vehicle #1"
- "Show all vehicles with service due"
- "What's the maintenance cost for this month?"
- "Search for blue vehicles"

### Manufacturing
- "Create BOM: Widget contains 3 Components A and 2 Components B"
- "Create manufacturing order: produce 50 Widgets"
- "Confirm production order #1"
- "What's the status of MO-001?"
- "Show all in-progress manufacturing orders"

### Calendar
- "Create meeting: Team Standup, tomorrow at 10am, 1 hour"
- "Show me my meetings for next week"
- "What events do I have on the 15th?"
- "Schedule a 2-hour planning session with the team"

### To-Do Priority Matrix
- "Create a to-do for Ian: Review Q4 budget, urgent and important, due April 15"
- "Show Ian's Eisenhower matrix"
- "Show the team workload dashboard"
- "List all overdue to-dos"
- "Complete to-do #42"
- "Show all Do First tasks for the team"
- "Create a to-do for Sarah: Update docs, important but not urgent"
- "Add checklist item 'Review code' to to-do #10"

### eCommerce
- "Publish Widget X to the website"
- "Show me website orders from this week"
- "What's my website revenue?"

## Smart Actions

The connector handles fuzzy/incomplete requests with intelligent find-or-create logic.

### How Smart Actions Work

**Example:** "Create quotation for Rocky with product Rock"

The system:
1. **Searches** for a customer named "Rocky" (case-insensitive, `ilike` matching)
2. **If not found**: Creates a new customer "Rocky" (auto-company flag)
3. **Searches** for product "Rock"
4. **If not found**: Creates a basic product "Rock" (consumable type, default price $0)
5. **Creates** the quotation, linking both the found/created customer and product
6. **Reports** what was found vs. created:
   - "Created quotation QT-001 for new customer Rocky with 1 × Rock at $0.00"

This pattern applies across all smart actions:
- `smart_create_quotation()` — customer + products
- `smart_create_purchase()` — vendor + products
- `smart_create_lead()` — partner (optional)
- `smart_create_task()` — project + task
- `smart_create_employee()` — department
- `smart_create_event()` — event only (no dependencies)
- `smart_create_todo()` — employee (fuzzy name match)
- `smart_get_matrix()` — employee (fuzzy name match)
- `smart_get_team_workload()` — team-wide dashboard

### Benefits

- **Fuzzy matching**: Searches are case-insensitive and forgiving
- **Auto-creation**: Missing dependencies are created automatically
- **Transparency**: Each response explains what was created vs. found
- **No IDs needed**: Use names instead of Odoo IDs
- **Batch operations**: Create multiple related records in one call

## Architecture

### Core Components

**OdooClient** — Low-level XML-RPC wrapper
- Connects to Odoo 19 instance
- Handles authentication via API key
- Provides `search()`, `read()`, `create()`, `write()`, `unlink()` methods
- Built-in retry logic and error handling

**Model Ops Classes** — Business logic for each module
- `PartnerOps` — Customers/suppliers
- `SaleOrderOps` — Quotations and sales orders
- `InvoiceOps` — Customer invoices
- `InventoryOps` — Products and stock
- `CRMOps` — Leads and opportunities
- `PurchaseOrderOps` — POs and vendors
- `ProjectOps` — Projects and tasks
- `HROps` — Employees, departments, expenses
- `ManufacturingOps` — BOMs and MOs
- `CalendarOps` — Events and meetings
- `FleetOps` — Vehicles and odometer
- `EcommerceOps` — Website orders and products
- `TodoMatrixOps` — To-Do Priority Matrix (Eisenhower)

**SmartActionHandler** — High-level natural-language interface
- Wraps all Ops classes
- Implements find-or-create workflows
- Fuzzy name matching (case-insensitive)
- Multi-step transaction orchestration
- Detailed response summaries

### Field Handling

The connector auto-detects required vs. optional fields in Odoo 19:
- **Implicit defaults**: Fields with Odoo defaults (e.g., state) are omitted
- **Smart creation**: Auto-fills reasonable defaults for optional fields
- **Error reporting**: Missing required fields raise clear `OdooError` with field name

## Configuration

### config.json Format

```json
{
  "url": "http://localhost:8069",
  "db": "your_database",
  "username": "api_user@yourcompany.com",
  "api_key": "your_api_key_from_odoo_preferences",
  "timeout": 60,
  "max_retries": 3,
  "poll_interval": 60,
  "log_level": "INFO",
  "webhook_port": 8070,
  "webhook_secret": ""
}
```

### Getting Your API Key

1. Log in to your Odoo instance
2. Go to **Settings** → **Users & Companies** → **Users**
3. Open your user record
4. Scroll to **Access Tokens**
5. Click **Generate Token**
6. Copy the token and paste into `config.json`

### Environment Variables

Alternatively, set in `.env`:

```
ODOO_URL=http://localhost:8069
ODOO_DB=your_database
ODOO_USERNAME=api_user@yourcompany.com
ODOO_API_KEY=your_api_key
```

The client auto-loads from `.env` if `config.json` is missing.

## Python API

### Basic Usage

```python
from odoo_skill import OdooClient, SmartActionHandler

# Load config from config.json
client = OdooClient.from_config("config.json")

# Test connection
status = client.test_connection()
print(f"Connected to Odoo {status['server_version']}")

# Use smart actions for natural workflows
smart = SmartActionHandler(client)

# Create a quotation with fuzzy partner and product matching
result = smart.smart_create_quotation(
    customer_name="Rocky",
    product_lines=[
        {"name": "Rock", "quantity": 5, "price_unit": 19.99}
    ],
    notes="Fuzzy match quotation"
)

print(result["summary"])
# Output: "Created quotation QT-001 for new customer Rocky with 1 × Rock at $19.99"
```

### Smart Actions API

```python
# Find-or-create a customer
result = smart.find_or_create_partner(
    name="Acme Corp",
    is_company=True,
    city="New York"
)
partner = result["partner"]
created = result["created"]

# Find-or-create a product
result = smart.find_or_create_product(
    name="Widget X",
    list_price=49.99,
    type="consu"
)
product = result["product"]

# Smart quotation (auto-creates customer & products)
result = smart.smart_create_quotation(
    customer_name="Rocky",
    product_lines=[
        {"name": "Product A", "quantity": 10},
        {"name": "Product B", "quantity": 5, "price_unit": 25.0}
    ],
    notes="Created via smart action"
)
order = result["order"]
print(f"Order {order['name']} created with {len(result['products'])} product(s)")

# Smart lead creation
result = smart.smart_create_lead(
    name="New Prospect",
    contact_name="John Doe",
    email="john@prospect.com",
    expected_revenue=50000.0
)
lead = result["lead"]

# Smart task creation (auto-creates project if needed)
result = smart.smart_create_task(
    project_name="Website Redesign",
    task_name="Fix homepage",
    description="Update hero section"
)
task = result["task"]

# Smart employee creation (auto-creates department if needed)
result = smart.smart_create_employee(
    name="Jane Smith",
    job_title="Developer",
    department_name="Engineering"
)
employee = result["employee"]

# Smart to-do creation (resolves employee by name)
result = smart.smart_create_todo(
    task_name="Review Q4 budget",
    employee_name="Ian",
    is_urgent=True,
    is_important=True,
    deadline="2026-04-15",
)
todo = result["task"]
print(result["summary"])
# Output: "To-do 'Review Q4 budget' created for Ian → Do First (urgent + important)"

# Get Eisenhower matrix for an employee
result = smart.smart_get_matrix(employee_name="Ian")
print(result["summary"])
# Output: "Priority Matrix for Ian: 3 Do First, 5 Schedule, 2 Delegate, 1 Eliminate (11 total)"

# Get team workload
result = smart.smart_get_team_workload()
print(result["summary"])
# Output: "Team Workload: 5 members, 23 active tasks, 4 overdue, 45.5h estimated"
```

### Low-Level Ops API

```python
from odoo_skill.models.sale_order import SaleOrderOps
from odoo_skill.models.partner import PartnerOps

partners = PartnerOps(client)
sales = SaleOrderOps(client)

# Get all customers
customers = partners.search_customers(limit=10)
for cust in customers:
    print(f"{cust['name']} — {cust.get('email')}")

# Create a quotation with specific IDs
order = sales.create_quotation(
    partner_id=42,
    lines=[
        {"product_id": 7, "quantity": 10, "price_unit": 49.99},
        {"product_id": 8, "quantity": 5}
    ],
    notes="Manual order"
)
print(f"Created {order['name']}")

# Confirm the order
confirmed = sales.confirm_order(order['id'])
print(f"Order {confirmed['name']} is now {confirmed['state']}")
```

## Response Format

All API methods return structured dictionaries:

### Smart Action Response

```python
{
  "summary": "Created quotation QT-001 for new customer Rocky with 1 × Rock",
  "order": {
    "id": 1,
    "name": "QT-001",
    "state": "draft",
    "partner_id": [42, "Rocky"],
    "amount_total": 19.99
  },
  "customer": {
    "created": True,
    "partner": {"id": 42, "name": "Rocky"}
  },
  "products": [
    {
      "created": True,
      "product": {"id": 7, "name": "Rock"}
    }
  ]
}
```

### Standard Response

```python
{
  "id": 1,
  "name": "QT-001",
  "state": "draft",
  "partner_id": [42, "Rocky"],
  "amount_total": 19.99,
  "order_line": [
    {
      "id": 1,
      "product_id": [7, "Rock"],
      "quantity": 1,
      "price_unit": 19.99,
      "price_subtotal": 19.99
    }
  ]
}
```

## Error Handling

The connector uses custom exceptions:

```python
from odoo_skill.errors import OdooError, OdooAuthError, OdooNotFoundError

try:
    result = smart.smart_create_quotation(
        customer_name="Acme",
        product_lines=[{"name": "Widget"}]
    )
except OdooAuthError as e:
    print(f"Authentication failed: {e}")
except OdooNotFoundError as e:
    print(f"Record not found: {e}")
except OdooError as e:
    print(f"Odoo error: {e}")
```

## Supported Odoo Modules

The connector supports 153+ installed modules in Odoo 19:

**Core**
- base, web, website

**Sales & CRM**
- sale, crm, sale_management, website_sale, event, survey

**Purchasing**
- purchase, purchase_stock, purchase_requisition

**Inventory**
- stock, stock_intrastat, stock_dropshipping

**Accounting**
- account, account_accountant, account_analytic, account_payment

**HR**
- hr, hr_attendance, hr_expense, hr_contract, hr_holidays, hr_org_chart

**Projects**
- project, project_enterprise, task_base, project_timesheet_forecast

**Manufacturing**
- mrp, mrp_byproduct, quality, batch, shelf_life

**Fleet**
- fleet, maintenance

**Marketing**
- marketing_automation, email_marketing, mass_mailing, sms, website_form

**eCommerce**
- website_sale, website_sale_analytics, website_sale_comparison, website_form_project

**Tools**
- calendar, documents, spreadsheet, discuss, mail, knowledge

**Plus 50+ more specialized modules**

## Limits & Constraints

- **Search limit**: 100 records by default (configurable)
- **Timeout**: 60 seconds per request (configurable)
- **Retries**: 3 automatic retries on network failure
- **Concurrency**: Single-threaded; queue requests if needed
- **Rate limiting**: Follow your Odoo instance's API limits

## Troubleshooting

### Connection Issues
- Verify `url`, `db`, `username`, `api_key` in config.json
- Check Odoo server is running: `http://your-odoo-url/web`
- Ensure API key is generated in Odoo user settings
- Check network connectivity and firewall rules

### Authentication Errors
- Regenerate API key in Odoo
- Verify username (email format)
- Check that the user has API access enabled
- Ensure database name matches exactly

### Missing Field Errors
- Field names must match Odoo 19 exactly (e.g., `product_tmpl_id`, not `product_id`)
- Some fields are read-only in Odoo (state, computed fields)
- Check Odoo model definition: Settings → Technical → Database Structure → Models

### Smart Action Issues
- Fuzzy matching is case-insensitive but searches only the `name` field
- For exact matching, use the low-level Ops API with `id` directly
- If a name exists in multiple records, the first match is used

### Performance
- Large searches (limit > 100) may timeout
- Use date range filters: `date_from`, `date_to`
- Consider batch operations for bulk data

## Examples in OpenClaw

### Natural Language Sales Order

```
User: "Create a quote for Acme Corp with 10 Widgets at $50 each"

OpenClaw → OdooClient (smart action):
  1. Search for customer "Acme Corp"
  2. Search for product "Widgets"
  3. Create quotation with both
  4. Return summary

Result: "✅ Created quotation QT-001 for Acme Corp with 10 × Widgets at $50"
```

### Pipeline Status Check

```
User: "Show me the sales pipeline"

OpenClaw → CRMOps.get_pipeline():
  - Query all leads/opportunities
  - Group by stage
  - Calculate total revenue by stage
  - Return formatted summary

Result: "Qualified: $50k | Proposal: $100k | Negotiation: $75k | Total: $225k"
```

### Inventory Alert

```
User: "What products are low on stock?"

OpenClaw → InventoryOps.get_low_stock_products():
  - Query products with stock < reorder point
  - List each product, stock level, reorder point
  - Suggest PO quantities

Result: "Widget X: 5 on hand (min 20) | Component Y: 0 on hand (min 10)"
```

## Development

### Project Structure

```
OdooConnector/
├── odoo_skill/
│   ├── client.py              # Core OdooClient
│   ├── config.py              # Configuration loader
│   ├── errors.py              # Custom exceptions
│   ├── retry.py               # Retry logic
│   ├── smart_actions.py       # Smart action handler
│   ├── models/
│   │   ├── partner.py
│   │   ├── sale_order.py
│   │   ├── invoice.py
│   │   ├── inventory.py
│   │   ├── crm.py
│   │   ├── purchase.py
│   │   ├── project.py
│   │   ├── hr.py
│   │   ├── manufacturing.py
│   │   ├── calendar_ops.py
│   │   ├── fleet.py
│   │   ├── ecommerce.py
│   │   ├── todo_matrix.py           # To-Do Priority Matrix (Eisenhower)
│   ├── utils/
│   │   ├── formatting.py      # Response formatting
│   │   ├── validators.py      # Input validation
│   ├── sync/
│   │   ├── poller.py          # Webhook poller
│   │   ├── webhook.py         # Webhook handler
├── run_full_test.py           # Integration test suite
├── config.json                # Configuration (create from template)
├── config.template.json       # Configuration template
├── requirements.txt           # Python dependencies
├── README.md                  # User setup guide
├── SKILL.md                   # This file
└── setup.ps1                  # PowerShell installer
```

### Running Tests

```bash
# Run full integration test suite
python run_full_test.py

# Run single test module
python -m pytest tests/test_partners.py -v

# Run with coverage
python -m pytest --cov=odoo_skill tests/
```

### Adding a New Smart Action

1. Implement the method in `SmartActionHandler` class
2. Use `find_or_create_*` primitives for dependencies
3. Return a dict with `summary`, the main record, and creation details
4. Add docstring with example usage
5. Test with `run_full_test.py`

Example:

```python
def smart_create_invoice(self, customer_name: str, product_lines: list[dict], **kwargs) -> dict:
    """Create invoice with fuzzy customer and product matching."""
    # Find or create customer
    customer_result = self.find_or_create_partner(customer_name)
    customer = customer_result["partner"]
    
    # Find or create products
    products = []
    for line in product_lines:
        prod_result = self.find_or_create_product(line["name"], **line)
        products.append(prod_result)
    
    # Create invoice with resolved IDs
    invoice = self.invoices.create_invoice(
        partner_id=customer["id"],
        lines=[...],
        **kwargs
    )
    
    return {
        "summary": f"Created invoice INV-001 for {customer['name']}",
        "invoice": invoice,
        "customer": customer_result,
        "products": products
    }
```

## License & Support

This connector is part of the OpenClaw project. For issues, questions, or contributions, contact the development team.

---

**Last Updated:** 2026-02-09  
**Odoo Version:** 19.0  
**Python:** 3.10+  
**Status:** Production Ready
