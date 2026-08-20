---
name: odoo
description: Manage Odoo 17 ERP via XML-RPC — use when the user wants to create, search, or manage sales orders, CRM leads, purchase orders, invoices, inventory, projects, HR, fleet, manufacturing, calendar events, to-do tasks, or the AndersonTech custom modules (repairs, RMAs, warranty, helpdesk, Facebook Marketplace listings, inbound packages, eBay messages, auction sourcing, product photography, PC builds) in their Odoo instance.
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

The skill resolves names to Odoo IDs using fuzzy (case-insensitive `ilike`) matching. Missing records are **not** created unless you pass `--allow-create`.

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
- **Auto-creation is opt-in** -- a lookup miss returns `needs_confirmation` with near matches and writes nothing. Pass `--allow-create` (CLI) or `allow_create=True` (Python) to create missing customers, products, vendors, projects, and departments. Changed in v3.0.0; previously creation was unconditional.
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


## Custom Modules (AndersonTech)

The subcommands above cover core Odoo. The AndersonTech custom modules add
~330 methods across 20 namespaces, reached through four generic commands
rather than one subcommand each.

```bash
python3 odoo.py list-ops                      # namespaces + whether each module is installed
python3 odoo.py list-ops repairs              # methods, signatures, and which ones write
python3 odoo.py describe-op repairs.create_repair
python3 odoo.py list-actions rmas             # allowlisted Odoo button methods
python3 odoo.py call repairs.bench_summary
python3 odoo.py call rmas.awaiting_approval --args '{"limit": 10}'
python3 odoo.py call repairs.create_repair \
  --args '{"partner_id": 42, "reported_problem": "No power"}' --confirm
```

Any method whose name implies a write (`create`, `update`, `set`, `post`,
`apply`, `publish`, `schedule`, `run_action`, ...) refuses to run without
`--confirm`. The refusal happens before any RPC.

Two methods mutate despite reading like queries, and are gated accordingly:
`ebay.research_comps` calls the eBay Browse API and writes recomputed comp
aggregates back to the product, and `smart.learn_location` persists an alias to
`location_vocab.json`. The full classification is frozen in
`tests/test_write_gate.py::EXPECTED_WRITES` — a name-based heuristic alone
missed both, so adding any ops method now fails that test until it is
classified deliberately.

| Namespace | Model | Module | Examples |
|---|---|---|---|
| `repairs` | `repair.order` | `atech_repair` | `bench_summary`, `overdue_repairs`, `awaiting_parts`, `find_by_serial`, `create_repair` |
| `rmas` | `rma.order` | `atech_rma` | `pipeline_summary`, `awaiting_approval`, `ready_to_execute`, `ebay_rmas`, `create_rma` |
| `warranty` | `warranty.registration` | `atech_warranty` | `check_coverage`, `expiring_soon`, `open_claims`, `create_claim` |
| `consignment` | `consignment.order` | `atech_consignment` | `pipeline_summary`, `items_awaiting_payout`, `set_pricing` |
| `helpdesk` | `helpdesk.ticket` | `atech_helpdesk` | `desk_summary`, `ebay_action_needed`, `draft_ai_reply` |
| `messaging` | `atech.conversation` | `atech_messaging` | `inbox`, `inbox_summary`, `unread`, `get_thread`, `reply` |
| `field_service` | `project.task` (FSM) | `atech_field_service` | `dispatch_board`, `schedule_job`, `unschedule_job`, `unscheduled_jobs` |
| `ebay` | `ebay.listing` | `sale_ebay` | `listing_summary`, `research_comps`, `get_pricing`, `apply_suggested_price`, `publish` |
| `product_drafts` | `quick.product.draft` | `quick_product`, `new_product_gui` | `attention_needed`, `stalled_drafts`, `ai_spend_summary` |
| `itad` | `tasks` | `projects-custom` | `ops_summary`, `upcoming_pickups`, `sla_at_risk`, `schedule_pickup` |
| `fb_marketplace` | `fb.marketplace.listing` | `fb_marketplace_lister` | `marketplace_summary`, `renewal_due`, `stale_listings`, `needs_content`, `mark_listed`, `mark_renewed` |
| `inbound` | `inbound.shipment` | `inbound_tracking` | `dashboard`, `action_queue`, `awaiting_confirmation`, `overdue`, `confirm_receipt`, `receive_line` |
| `order_status` | `sale.order` | `atech_order_status` | `status_link`, `awaiting_signature`, `confirmation_not_sent`, `settings` |
| `ebay_messages` | `ebay.message` | `odoo-ebay-messages` | `inbox_summary`, `aging`, `draft_reply`, `send_reply`, `unshipped_orders` |
| `auctions` | `auction.lot` | `auction_scrapper_catalog` | `sourcing_summary`, `ending_soon`, `needs_approval`, `over_ceiling`, `approve_bid` |
| `photography` | `photo.session` | `product_photography` | `studio_summary`, `stranded_lines`, `awaiting_review`, `close_session` |
| `pc_builds` | `pc.build` | `pc_configurator` | `configurator_summary`, `incompatible_builds`, `add_component`, `create_quotation` |

### Field lists adapt to the database

Optional modules add fields to models that exist everywhere: `helpdesk_repair`
puts `ticket_id` on `repair.order` and `repair_ids` on `helpdesk.ticket`;
`atech_messaging` puts `sms_fsm_*` on `project.task`. Those are installed on
staging and not on production.

Odoo's `read()` rejects an unknown field outright rather than skipping it, so
one such name in a class's `DETAIL_FIELDS` makes **every** `get()` on that
namespace raise — while `search`, the queues and the summaries keep working,
because they use `LIST_FIELDS`. That asymmetry is why it goes unnoticed.

`BaseOps` therefore intersects its declared lists with the fields the database
actually has (reusing the describe `available()` already performs, so no extra
round-trip) and logs what it dropped. Declarations stay complete; each database
gets what it can serve. An explicit `fields=` from a caller is **not** filtered
— a typo there should surface as an error.

`tests/test_live_fields.py` pins this against a real database; it skips unless
`ODOO_URL` / `ODOO_API_KEY` are set.

### Group-gated modules

Two modules ship restrictive `ir.model.access` rows, so an API user outside
their groups gets an access fault on **every** call — there is no partial
read, and the fault text is a wall of group names. Every ops class inherits
`access_check()`, which collapses "module missing" and "user lacks the group"
into one answer naming the group to grant:

```bash
python3 odoo.py call fb_marketplace.access_check
python3 odoo.py call photography.access_check
```

| Namespace | Groups the API user needs |
|---|---|
| `fb_marketplace` | `fb_marketplace_lister.group_fb_marketplace_user` (or `…_manager`) |
| `photography` | `product_photography.group_photo_user` (or `…_manager`) |

`monitor_testing` is deliberately **not** covered by a namespace: it defines
no `ir.model.access` rows at all, so its models are unreachable over XML-RPC
regardless of group membership. That is by design (the module is fed by its
own station API), not a gap to fill.

### Outward-facing actions are two-step

Anything that messages a real customer is split so an agent cannot do it in
one move. `ebay_messages.draft_reply` generates a draft and sends nothing;
`send_reply` requires the body to be passed explicitly rather than flushing
whatever a previous step left in `reply_draft`. The same split already
governs `helpdesk.draft_ai_reply`.

`order_status.status_link` returns a URL containing a live capability token.
`status_token` is deliberately absent from the list and detail field sets, so
listing orders never sprays customer links into a transcript — the link is
produced one order at a time, on request.

### Refusals that are not errors

Some calls return a refusal envelope (`{"ok": false, "summary": ...}`) rather
than acting, because acting would produce a quietly wrong record:

| Call | Refuses when | Override |
|---|---|---|
| `pc_builds.create_quotation` / `create_build_order` | the build has compatibility errors | `override=True` |
| `photography.close_session` | lines are still off the shelf | `force=True` |
| `auctions.approve_bid` | `max_bid` is not positive | none — raises |
| `ebay_messages.send_reply` | the body is empty | none — raises |

### Odoo button methods

Status transitions go through the module's own button methods, never a raw
write to `state`. On `repair.order` this is mandatory — `state` is computed
from `stage_id` and is readonly. Elsewhere it is still correct, because
writing `state` directly skips the side effects (emails, pickings, resolution
execution).

Each ops class carries an explicit `ALLOWED_ACTIONS` allowlist, because
`execute_kw` will otherwise invoke *any* public method including `unlink`.
Deliberately excluded: `quick.product.draft.action_commit` (creates a
permanent catalogue product) and all ITAD buttons (compliance weight).

### Button methods that return nothing

Odoo's XML-RPC controller serialises responses with `allow_none=False`, and
that is hardcoded server-side — no client setting changes it. A button method
that ends without a `return` therefore produces:

```
TypeError: cannot marshal None unless allow_none is enabled
```

**The call succeeded and its transaction committed.** Only encoding the reply
failed. Verified against live Odoo 17: `fb.marketplace.listing.action_mark_listed`
raises this, and re-reading the record shows `state == 'listed'` with
`listed_date` stamped.

`OdooClient.execute` matches this one fault narrowly and returns `None`, so
`run_action` reports `"returned": null` alongside the record's real
post-action state. Every other fault still raises. Surfacing it as an error
would be worse than useless — a caller retries an action that already ran, or
reports a failure that did not happen.

### Dispatch payloads (one round-trip)

Two ops wrap the same model-level methods the Odoo UIs call, returning a
whole working surface in a single request instead of a dozen searches:

```bash
python3 odoo.py call messaging.inbox --args '{"view": "unassigned"}'
python3 odoo.py call messaging.inbox --args '{"search": "dell latitude"}'
python3 odoo.py call field_service.dispatch_board --args '{"days": 7}'
```

`messaging.inbox` returns conversation cards, counts across every lane, the
agent roster and canned responses. `view` is a *lane*, not a status —
`mine` and `unassigned` cut across statuses, and `mine` resolves against the
**authenticated API user**, not whoever the agent is acting for. A `search`
spans all statuses and overrides `view`; queries under 2 characters are
ignored by the module to avoid a full message-body scan.

`field_service.dispatch_board` returns technicians, day columns, the
unscheduled backlog and scheduled cards, timezone-resolved. It requires the
API user to be in `industry_fsm.group_fsm_user` — the module enforces that
on every dispatch RPC, so a missing group surfaces as an Odoo `AccessError`.

Scheduling **notifies the customer** (`_fsm_notify_scheduled`), so it is
gated on an explicit `confirm`:

```bash
python3 odoo.py call field_service.schedule_job \
  --args '{"task_id": 812, "date": "2026-08-03", "user_id": 25, "confirm": true}' --confirm
python3 odoo.py call field_service.unschedule_job --args '{"task_id": 812}' --confirm
```

Odoo returns a bare `False` from `dispatch_assign` when it refuses (the task
is not FSM, or the user is not a technician) — that is reported as
`status: "refused"`, never as success. `dispatch_unassign` is worse: it
returns `True` unconditionally, even when it changes nothing, so
`unschedule_job` confirms by reading the dates back.

### eBay repricing is proposal-first

The pricing engine lives in Odoo (`sale_ebay`), not here — it researches
comps and computes a suggestion clamped by a cost floor
(`sale_ebay.reducer_min_margin`). Reading a suggestion is free; applying it
is separate and guarded three ways: no actionable suggestion, a cut above
`max_discount_pct` (default 25%), or a missing inner `confirm: true` all
refuse.

```bash
python3 odoo.py call ebay.repricing_candidates --args '{"min_days_listed": 14}'
python3 odoo.py call ebay.get_pricing --args '{"product_tmpl_id": 4211}'
python3 odoo.py call ebay.apply_suggested_price \
  --args '{"product_tmpl_id": 4211, "confirm": true}' --confirm
```

`stale_comps` lists listed products whose comps have never been fetched —
their suggestions mean nothing until `research_comps` runs.

### Unsearchable computed fields

Some custom fields are computed and not stored. A domain over an
**unsearchable** one is not rejected — Odoo drops the clause and returns the
*unfiltered* set, producing plausible but wrong answers. It logs an error
server-side, but nothing reaches the RPC caller. These are filtered
client-side instead, over a bounded scan:

| Model | Field |
|---|---|
| `repair.order` | `is_overdue`, `is_awaiting_parts` |
| `rma.order` | `can_execute_resolutions` |
| `tasks` (ITAD) | `itad_can_dispatch`, `itad_can_price`, `itad_can_receive`, `sla_days_remaining` |
| `fb.marketplace.listing` | `days_listed` |
| `ebay.message` | `order_id` |
| `pc.build` | `has_speculative_parts` |
| `photo.session.line` | `minutes_at_studio` |
| `repair.part.line` | `state`, `qty_received` |
| `photo.digitization` | `attempt_count` |

**The discriminator is `searchable`, not `store`.** A non-stored field is
still searchable when it is `related=` to a stored one, or when its
definition supplies a `search=` method — Odoo then rewrites the domain and
resolves it server-side, correctly. Filtering those client-side is strictly
worse: the scan is capped at `COMPUTED_SCAN_CAP` rows and silently
under-reports past it, where a server-side domain is exact.

Non-stored but searchable — filter these server-side:
`project.task.is_fsm` (related to `project_id.is_fsm`) and
`rma.order.advance_return_overdue` (has `_search_advance_overdue`).

`quick.product.draft.price_confidence_pct` reads better through its stored
twin `price_confidence`. `helpdesk.ticket.ebay_order_number` is searchable
(`related="ebay_order_id.order_id"`) — filter it directly. Note
`ebay.order` has **no** `name` field, so `ebay_order_id.name` raises.

Before adding any filter:

```python
client.fields_get("rma.order", attributes=["store", "searchable"])
```

and sanity-check that the filtered count actually differs from the
unfiltered one.

> Beware of narrowing clauses that merely *look* related. `advance_return_overdue`
> is driven by per-line `resolution == 'replace_advance'`, **not** by the
> order-level `return_method` — pairing the two hid a real overdue RMA.

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

- `OdooError` -- base class for all Odoo errors
- `OdooConnectionError` -- network failure after retries
- `OdooAuthenticationError` -- bad credentials or expired API key
- `OdooAccessError` -- authenticated but not permitted
- `OdooValidationError` -- Odoo rejected the values
- `OdooRecordNotFoundError` -- record does not exist
- `OdooModuleNotInstalledError` -- namespace used on a DB lacking its module
- `OdooActionNotAllowedError` -- method outside the class allowlist

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
