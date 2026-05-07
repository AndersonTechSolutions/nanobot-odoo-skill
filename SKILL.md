---
name: odoo
description: Manage Odoo 17 ERP via XML-RPC — use when the user wants to create, search, or manage sales orders, CRM leads, purchase orders, invoices, inventory, projects, HR, fleet, manufacturing, calendar events, or to-do tasks in their Odoo instance.
metadata: {"nanobot":{"emoji":"🏢","requires":{"bins":["python3"]}}}
---

# Odoo ERP Skill

Odoo 17 ERP integration for nanobot. Control sales, CRM, purchasing, inventory, invoicing, projects, HR, fleet, manufacturing, calendar, and to-do tasks via XML-RPC.

## Setup

### Required Environment Variables

| Variable | Description |
|---|---|
| `ODOO_URL` | Odoo instance URL (e.g. `https://mycompany.odoo.com`) |
| `ODOO_DB` | Database name |
| `ODOO_USERNAME` | API user email |
| `ODOO_API_KEY` | API key from Odoo user preferences |

### Getting an API Key

1. Log in to your Odoo instance
2. Go to **Settings > Users & Companies > Users**
3. Open your user record, scroll to **Access Tokens**
4. Click **Generate Token** and copy the key

### Install Dependencies

```bash
pip install -r skills/odoo/requirements.txt
```

## CLI Entry Point

```bash
python3 skills/odoo/odoo.py "<command>"
```

All commands accept natural language. The skill resolves names to Odoo IDs automatically using fuzzy (case-insensitive `ilike`) matching and creates missing records when needed.

## Command Reference

| Subcommand | Trigger phrase | Description |
|---|---|---|
| Create quotation | `create quotation`, `create quote` | Create a sales quotation with customer and product lines |
| Confirm order | `confirm order`, `confirm quotation` | Confirm a draft sales order |
| Create lead | `create lead` | Create a CRM lead with optional partner linking |
| Create opportunity | `create opportunity` | Create a CRM opportunity |
| Create purchase | `create purchase`, `create po` | Create a purchase order from a vendor |
| Check stock | `check stock`, `stock level` | Query product stock levels |
| Create task | `create task` | Create a project task (auto-creates project if needed) |
| Create employee | `create employee` | Create an HR employee (auto-creates department if needed) |
| Create to-do | `create todo`, `create to-do` | Create a to-do in the Eisenhower priority matrix |
| To-do matrix | `todo matrix`, `eisenhower`, `priority matrix` | View an employee's Eisenhower matrix |
| Team workload | `team workload`, `workload dashboard` | View team workload dashboard |
| Complete to-do | `complete todo`, `done todo` | Mark a to-do task as done |
| List to-dos | `list todo`, `show todo`, `my todo` | List to-do tasks with optional filters |
| Create invoice | via Python API | Create an invoice with fuzzy customer/product resolution |
| Create event | via Python API | Create a calendar event with attendee resolution |
| Search orders | via Python API | Search sales orders by status, customer, date |
| Search invoices | via Python API | Query unpaid/overdue invoices |
| Search products | via Python API | Search products by name, code, or category |
| Search employees | via Python API | Search employees by name, department, job |
| Get pipeline | via Python API | View CRM pipeline with revenue by stage |

## Smart Actions

All smart actions follow the same pattern: search first, create only if needed, report what was found vs. created.

- **Fuzzy matching** -- case-insensitive `ilike` searches on the `name` field
- **Auto-creation** -- missing customers, products, vendors, projects, and departments are created on the fly
- **No IDs needed** -- use names everywhere; the skill resolves them to Odoo record IDs

## Example Usage

### Create a quotation

```bash
python3 skills/odoo/odoo.py "create quotation for Acme Corp with 10 Widgets at \$50 each"
```

The skill will:
1. Search for customer "Acme Corp" (create if not found)
2. Search for product "Widgets" (create if not found)
3. Build the quotation linking both

### Create a CRM lead

```bash
python3 skills/odoo/odoo.py "create lead for Rocky, email rocky@example.com"
```

### Create a to-do with priority

```bash
python3 skills/odoo/odoo.py "create todo Review Q4 budget for Ian, urgent and important"
```

### View Eisenhower matrix

```bash
python3 skills/odoo/odoo.py "todo matrix for Ian"
```

### Check stock levels

```bash
python3 skills/odoo/odoo.py "check stock for Widget X"
```

## Python API

For direct programmatic access, import the skill:

```python
from odoo_skill import OdooClient, SmartActionHandler

client = OdooClient.from_env()
smart = SmartActionHandler(client)

result = smart.smart_create_quotation(
    customer_name="Rocky",
    product_lines=[{"name": "Rock", "quantity": 5, "price_unit": 19.99}],
)
print(result["summary"])
```

See `references/python-api.md` for the full API reference covering all Ops classes and SmartActionHandler methods.

## Error Handling

The skill uses custom exceptions from `odoo_skill.errors`:

- `OdooAuthError` -- bad credentials or expired API key
- `OdooNotFoundError` -- record does not exist
- `OdooError` -- general Odoo XML-RPC error

All CLI responses are JSON with either `{"success": true, "result": ...}` or `{"error": "...", "type": "..."}`.

## Notes

- Search results are capped at 100 records by default
- Requests timeout after 60 seconds (configurable)
- 3 automatic retries on network failure
- Targets Odoo 17; may work with 16+ but untested

## Location Resolution

### When to use
- A location string is rejected or doesn't match during `create-task` or `update-task`.
- The user gives a shorthand bin/aisle code, a conversational phrase, or a label that differs from Odoo's internal name.
- A task description mixes an Odoo location with a physical placement instruction (e.g., "put it on the metal cart").
- You need to set or clear a task location and want to confirm CLI capability.

### Steps

1. **Separate Odoo location from physical placement notes.** The `--location` field stores a stock location Odoo recognizes. Instructions like "put it on the metal cart" belong in the task description, not the location field.

2. **Normalize the location string.**
   - Try the user's value first. If it fails, check for punctuation and spacing differences.
     - Known: `MR-13C` fails → `MR13-C` succeeds.
   - If a compact bin code fails, expand to the warehouse path format Odoo expects.
     - Known: `040301B` → `AT_WH/WH Stock/04-03-01-B`; reuse canonical forms like `AT_WH/WH Stock/02-02-01`.
   - For aisle codes: `aisle 01` → `AT_WH/01` (ID 421); conversational variants like `aisle one` do not match.

3. **Confirm the target location exists before updating.** Use the Odoo CLI or search to find the internal location. Known: `Front Lobby` exists (ID 420).

4. **Use the correct CLI flags.**
   - `update-task ... --location "<value>"` — sets the location.
   - `update-task ... --no-location` — clears the location.

5. **Treat CLI success as the primary confirmation.** `get-task` does *not* display the location field — absence in readback is not proof of failure.

6. **Report clearly.** Show the user's original value, the internal Odoo value that worked, any physical placement note as a separate item, and note that readback won't visually confirm the location.

### Example

User: "Put task 119 in MR-13C and note to leave it on the metal cart."

- `MR-13C` → retry with `MR13-C` (punctuation shift).
- "leave it on the metal cart" → task description note, not location field.
- Run `update-task --task-id 119 --location "MR13-C"`.
- Report success; note that `get-task` won't show the location field.

---

## Name Resolution

### When to use
- An Odoo operation fails to match an employee by the name the user gave.
- Before concluding the record does not exist.

### Steps

1. If the full display name fails, retry with the shorter form used in Odoo.
   - Known: `Ian Anderson` fails → `Ian` succeeds.
2. Use the shortest identifier that resolves without ambiguity.
3. If matching still fails, report exact inputs tested. Distinguish "not found" from "natural-language variant didn't match" and offer the canonical working value for future requests.

### Example

User: "Create a task for Ian Anderson."

Retry with `Ian`. Report success and note the preferred short-form for future requests.

---

## Multi-Assignee Fallback

### When to use
- The user wants one task shared by two employees.
- The current Odoo CLI only supports a single assignee per task.

### Steps

1. Identify intent: individual accountability (separate tasks per person) or one shared task record?
2. If shared: keep one task, assign to one person. Do not imply both are assigned.
3. If forced to pick one assignee without explicit user instruction, use current precedent: shared-style tasks default to **Jasmine Martinez** as assignee (established when Martin's duplicate was deleted). Follow fresh user instructions if given.
4. Write the result clearly: state whether you created duplicates or kept one single-assignee task, and name the assignee.

### Example

User: "Make one task for Jasmine and Martin."

Keep one task, assign to Jasmine Martinez. State: "One shared-style task created under Jasmine Martinez — the Odoo CLI doesn't support true dual-assignment, so Martin is not set as an assignee."
