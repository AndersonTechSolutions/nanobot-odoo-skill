# MISTAKES.md

## Enforced Rules (check every task)

## Patterns (promote at 3 hits)

## Observations (first sightings)
- 2026-09-06: Added a `read_*` ops method that writes (`fb_marketplace.read_scale` stores the scale reading) → `_op_writes` gates by verb prefix and `read_` is not one, so it would have run without `--confirm` → any method whose name reads like a query but mutates goes in `_WRITE_EXACT` in `odoo.py` AND in `writes` of `tests/method_inventory.json` in the same commit; `test_write_gate` only catches the inventory half. (hits: 1)
- 2026-09-06: `_stage_router` in `tests/test_ebay_listing.py` answers `(product.template, read)` by field-set; a new read in `stage_listing` with a different field set silently falls through to the base template dict → route it explicitly in the test (see `test_fallback_only_fills_what_the_resolver_left_blank`) rather than trusting the fall-through. (hits: 1)
- 2026-09-06: Detecting "server lacks method X" from an `OdooError` by `X in str(exc)` is wrong — `classify_error` prefixes every message with `on <model>.<method>`, so the name is ALWAYS present → match Python's `no attribute 'X'` wording instead. (hits: 1)
