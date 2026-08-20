# Test Results

## Unit tests

```
$ python3 -m pytest tests/ -q
255 passed
```

| Suite | Covers |
|---|---|
| `test_config.py` | Env/config-file loading and validation |
| `test_client.py` | XML-RPC wrapper, error classification, null-return marshalling |
| `test_base_ops.py` | Module guards, action allowlist, computed-field scans |
| `test_smart_actions.py` | Fuzzy resolution and composite smart actions |
| `test_allow_create.py` | Opt-in auto-creation (3.0.0 breaking change) |
| `test_cli.py` | Subcommand parsing and output envelopes |
| `test_dispatch_ops.py` | `@api.model` wire shape (no ids list) |
| `test_new_connectors.py` | FB Marketplace, inbound, auctions, eBay messages, PC builds, order status, photography |
| `test_write_gate.py` | `--confirm` classification, frozen against a full method inventory |
| `test_live_fields.py` | live: declared fields exist and `get()` works (skipped without credentials) |

## Live verification (Odoo 17)

Read paths were exercised against both databases; write paths against staging
only, with the test records deleted afterwards.

### staging-atech.cloudpepper.site

| Group | Result |
|---|---|
| Original 13 custom-module namespaces (39 checks) | all pass |
| 7 new namespaces + deepened repair/helpdesk (68 checks) | all pass |
| FB Marketplace write lifecycle | create → mark_listed → mark_renewed → mark_sold → reset_draft → add_image, all pass; test record deleted |
| Action allowlist | `run_action(..., "unlink")` correctly refused |

### atech.cloudpepper.site (production, read-only)

Three environmental gaps, each reported by `access_check()` rather than a raw
fault. No code changes are needed for any of them:

| Namespace | Finding | Action |
|---|---|---|
| `fb_marketplace` | API user not in `fb_marketplace_lister.group_fb_marketplace_user` | grant the group |
| `photography` | API user not in `product_photography.group_photo_user` | grant the group |
| `auctions` | `auction_scrapper_catalog` not installed on prod (staging-only) | none — degrades cleanly |
| `itad` | API user cannot read the related `itad.order.service.line`, so `itad.get()` raises | grant `ITAD / User` |

`atech_order_status.status_page_url` **is** set on prod
(`https://andersontechsolutions.com/order-status/`) and `order_status.settings()`
reports `usable: true` there; `status_link()` builds a correct customer URL.
It is unset on **staging** only, which is a staging config gap, not a prod one.

### Not covered

`monitor_testing` defines **no** `ir.model.access` rows for any of its 18
models, so every model is unreachable over XML-RPC regardless of group
membership. That is by design — the module is fed by its own station API — so
no namespace was added. It is also staging-only.
