# Test Results

## Unit tests

```
$ python3 -m pytest tests/ -q
264 passed, 30 skipped
```

(30 skipped are the live checks in `test_live_fields.py`, which run only with
Odoo credentials in the environment — see below. The `messaging` and `auctions`
namespaces were removed — incomplete modules, not needed for communication — so
their connectors, tests, and inventory entries are gone.)

| Suite | Covers |
|---|---|
| `test_config.py` | Env/config-file loading and validation |
| `test_client.py` | XML-RPC wrapper, error classification, null-return marshalling |
| `test_base_ops.py` | Module guards, action allowlist, computed-field scans, live-schema field intersection |
| `test_smart_actions.py` | Fuzzy resolution and composite smart actions |
| `test_allow_create.py` | Opt-in auto-creation (3.0.0 breaking change) |
| `test_cli.py` | Subcommand parsing and output envelopes |
| `test_dispatch_ops.py` | `@api.model` wire shape (no ids list) |
| `test_new_connectors.py` | FB Marketplace, inbound, auctions, eBay messages, PC builds, order status, photography (incl. the atomic close guard + advisory fallback) |
| `test_write_gate.py` | `--confirm` classification, frozen against a full method inventory |
| `test_live_fields.py` | live: declared fields exist and `get()` works (skipped without credentials) |

## Live verification (Odoo 17)

Read paths exercised against both databases; write paths against staging only.

### Field existence — `test_live_fields.py`

```
staging-atech.cloudpepper.site:  30 passed,  4 skipped
atech.cloudpepper.site (prod):   24 passed,  1 failed, 9 skipped
```

The one prod failure is `ITADOps.get()` — the API user (`martin@`) can read
`project.task` but not the related `itad.order.service.line` it reads through,
so the read raises. Environmental (a group grant), not a field/code bug. The
`FieldServiceOps` fix (dropping `partner_email`/`kanban_state`, which prod's
`project.task` lacks) holds on both databases.

### Namespace reachability — `access_check()` across all 15 BaseOps namespaces

```
staging:  15/15 reachable — 0 gaps
prod:     13/15 reachable — 2 gaps (all environmental)
```

Prod gaps, and what each needs (see `docs/` / the delegation checklist):

| Namespace | Model | Gap | Fix |
|---|---|---|---|
| `fb_marketplace` | `fb.marketplace.listing` | no_access | grant `fb_marketplace_lister.group_fb_marketplace_user` to `martin@` |
| `photography` | `photo.session` | no_access | grant `product_photography.group_photo_user` to `martin@` |
| `itad` (`get()` only) | `itad.order.service.line` | no_access | grant `ITAD / User` to `martin@` |

`monitor_testing` defines no `ir.model.access` rows for any model (fed by its
own station API), so no namespace was added — by design, staging-only.

## Close-session race (server-side fix)

`photo.session.action_end_guarded` (in `product_photography` ≥ 17.0.5.1.0)
makes `close_session` atomic: the off-shelf count and the close happen in one
transaction behind a `FOR UPDATE` row lock, and the pickup transitions refuse a
closed session. Coverage lives in the module repo
(`product_photography/tests/test_session_close_guard.py`): single-transaction
semantics plus a committed two-cursor race asserting a pickup that commits after
the close's snapshot forces a `SerializationFailure` (which Odoo retries),
instead of the close silently stranding stock. Those tests run under an Odoo
instance with `--test-enable`; they are not part of this skill's offline suite.
The skill side (`test_new_connectors.py::TestPhotography`) covers the primary
atomic path, `force` forwarding, real-fault propagation, and the advisory
fallback for databases that predate the method.
