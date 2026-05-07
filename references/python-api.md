# Odoo Skill Python API Reference

## SmartActionHandler

High-level interface with fuzzy name matching and auto-creation workflows.

```python
from odoo_skill import OdooClient, SmartActionHandler

client = OdooClient.from_env()
smart = SmartActionHandler(client)
```

### smart_create_quotation(customer_name, product_lines, notes=None)

Create a sales quotation. Resolves customer and products by name.

```python
result = smart.smart_create_quotation(
    customer_name="Rocky",
    product_lines=[
        {"name": "Rock", "quantity": 5, "price_unit": 19.99},
        {"name": "Pebble", "quantity": 20, "price_unit": 1.50},
    ],
    notes="Fuzzy match quotation"
)
# result keys: order, customer, products, summary
```

### smart_create_invoice(customer_name, lines, invoice_date=None)

Create a customer invoice with fuzzy matching.

```python
result = smart.smart_create_invoice(
    customer_name="Acme Corp",
    lines=[{"name": "Consulting", "price_unit": 500, "quantity": 8}]
)
# result keys: invoice, customer, products, summary
```

### smart_create_lead(name, contact_name=None, email=None, phone=None, expected_revenue=None)

Create a CRM lead with optional partner linking.

```python
result = smart.smart_create_lead(
    name="New Prospect",
    contact_name="John Doe",
    email="john@prospect.com",
    expected_revenue=50000.0
)
# result keys: lead, partner, summary
```

### smart_create_purchase(vendor_name, product_lines, date_planned=None)

Create a purchase order. Resolves vendor and products by name.

```python
result = smart.smart_create_purchase(
    vendor_name="Supplier ABC",
    product_lines=[{"name": "Widget", "quantity": 500, "price_unit": 5.0}]
)
# result keys: purchase_order, vendor, products, summary
```

### smart_create_task(project_name, task_name, description=None, date_deadline=None, assignee_name=None)

Create a project task. Auto-creates project if not found.

```python
result = smart.smart_create_task(
    project_name="Website Redesign",
    task_name="Fix homepage",
    description="Update hero section"
)
# result keys: task, project, assignee, summary
```

### smart_create_employee(name, job_title=None, department_name=None, work_email=None, work_phone=None)

Create an employee. Auto-creates department if not found.

```python
result = smart.smart_create_employee(
    name="Jane Smith",
    job_title="Developer",
    department_name="Engineering"
)
# result keys: employee, department, created, summary
```

### smart_create_event(name, start, end=None, location=None, attendee_names=None)

Create a calendar event. Resolves attendees by name.

```python
result = smart.smart_create_event(
    name="Team Standup",
    start="2026-04-10 10:00:00",
    end="2026-04-10 11:00:00",
    attendee_names=["Alice", "Bob"],
    location="Conference Room A"
)
# result keys: event, attendees, summary
```

### smart_create_todo(task_name, employee_name, is_urgent=False, is_important=False, description=None, deadline=None, estimated_time=None)

Create a to-do task in the Eisenhower priority matrix.

```python
result = smart.smart_create_todo(
    task_name="Review Q4 budget",
    employee_name="Ian",
    is_urgent=True,
    is_important=True,
    deadline="2026-04-15"
)
# result keys: task, employee, quadrant, summary
```

### smart_get_matrix(employee_name)

Get an employee's Eisenhower priority matrix.

```python
result = smart.smart_get_matrix(employee_name="Ian")
# result keys: matrix, employee, summary
```

### smart_get_team_workload()

Get team workload dashboard.

```python
result = smart.smart_get_team_workload()
# result keys: workload, summary
```

### find_or_create_partner(name, is_company=True, supplier=False)

Low-level find-or-create for partners.

```python
result = smart.find_or_create_partner("Acme Corp", is_company=True)
# result keys: partner, created, matched
```

### find_or_create_product(name, **defaults)

Low-level find-or-create for products.

```python
result = smart.find_or_create_product("Widget X", list_price=49.99, type="consu")
# result keys: product, created, matched
```

## Low-Level Ops Classes

Available via `smart.<ops_name>` or by direct import:

```python
from odoo_skill.models.sale_order import SaleOrderOps
from odoo_skill.models.partner import PartnerOps

partners = PartnerOps(client)
sales = SaleOrderOps(client)
```

### PartnerOps
- `search_customers(query=None, limit=20)` — Search customers
- `create_customer(name, email=None, phone=None, **kwargs)` — Create customer

### SaleOrderOps
- `create_quotation(partner_id, lines, notes=None)` — Create quotation
- `confirm_order(order_id)` — Confirm sales order
- `cancel_order(order_id)` — Cancel sales order
- `get_order(order_id)` — Get order details
- `search_orders(partner_id=None, state=None, limit=20)` — Search orders
- `get_order_lines(order_id)` — Get order line items

### CRMOps
- `create_lead(name, contact_name=None, email=None, phone=None, expected_revenue=None)` — Create lead
- `create_opportunity(name, partner_id=None, expected_revenue=None)` — Create opportunity
- `get_pipeline()` — Get CRM pipeline
- `move_stage(lead_id, stage_id)` — Move lead to stage
- `mark_won(lead_id)` — Mark opportunity as won
- `mark_lost(lead_id, lost_reason=None)` — Mark as lost
- `get_stages()` — List CRM stages

### InventoryOps
- `search_products(query, limit=20)` — Search products
- `check_product_availability(product_name)` — Check stock levels
- `get_stock_levels(product_id=None, location_id=None)` — Get stock quants
- `get_low_stock_products(threshold=10)` — List low-stock products

### PurchaseOrderOps
- `create_purchase_order(partner_id, lines, date_planned=None)` — Create PO
- `confirm_purchase(order_id)` — Confirm PO
- `search_purchase_orders(partner_id=None, state=None, limit=20)` — Search POs

### InvoiceOps
- `create_invoice(partner_id, lines, invoice_date=None)` — Create invoice
- `get_invoice(invoice_id)` — Get invoice details
- `search_invoices(partner_id=None, state=None, limit=20)` — Search invoices

### ProjectOps
- `create_project(name, **kwargs)` — Create project
- `create_task(project_id, name, user_ids=None, description=None, date_deadline=None)` — Create task
- `search_tasks(project_id=None, state=None, limit=20)` — Search tasks

### HROps
- `create_employee(name, job_title=None, department_id=None, work_email=None, work_phone=None)` — Create employee
- `create_department(name)` — Create department
- `search_employees(query=None, department_id=None, limit=20)` — Search employees

### CalendarOps
- `create_event(name, start, stop=None, allday=False, location=None, partner_ids=None)` — Create event
- `search_events(date_from=None, date_to=None, limit=20)` — Search events

### TodoMatrixOps
- `create_task(name, employee_id, is_urgent=False, is_important=False, description=None, deadline=None, estimated_time=None)` — Create to-do
- `complete_task(task_id)` — Complete to-do
- `search_tasks(employee_id=None, state=None, quadrant=None)` — Search to-dos
- `get_matrix(employee_id)` — Get employee matrix
- `get_team_workload()` — Get team dashboard

### FleetOps, ManufacturingOps, EcommerceOps
See source files in `odoo_skill/models/` for full API.

## Error Handling

```python
from odoo_skill.errors import OdooError, OdooAuthError, OdooNotFoundError

try:
    result = smart.smart_create_quotation(...)
except OdooAuthError:
    # Bad credentials or expired API key
except OdooNotFoundError:
    # Record not found
except OdooError:
    # General Odoo XML-RPC error
```

## Response Format

All smart actions return dicts with a `summary` key (human-readable) plus the created/found records:

```python
{
    "summary": "Quotation S00042 created for Acme Corp with 2 line(s)",
    "order": {"id": 42, "name": "S00042", "state": "draft", ...},
    "customer": {"partner": {...}, "created": False},
    "products": [{"product": {...}, "created": True}, ...]
}
```
