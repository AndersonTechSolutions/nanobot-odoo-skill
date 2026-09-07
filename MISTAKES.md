# MISTAKES.md

## Enforced Rules (check every task)

## Patterns (promote at 3 hits)

## Observations (first sightings)
- 2026-09-06: Added a `read_*` ops method that writes (`fb_marketplace.read_scale` stores the scale reading) → `_op_writes` gates by verb prefix and `read_` is not one, so it would have run without `--confirm` → any method whose name reads like a query but mutates goes in `_WRITE_EXACT` in `odoo.py` AND in `writes` of `tests/method_inventory.json` in the same commit; `test_write_gate` only catches the inventory half. (hits: 1)
- 2026-09-06: `_stage_router` in `tests/test_ebay_listing.py` answers `(product.template, read)` by field-set; a new read in `stage_listing` with a different field set silently falls through to the base template dict → route it explicitly in the test (see `test_fallback_only_fills_what_the_resolver_left_blank`) rather than trusting the fall-through. (hits: 1)
- 2026-09-06: Detecting "server lacks method X" from an `OdooError` by `X in str(exc)` is wrong — `classify_error` prefixes every message with `on <model>.<method>`, so the name is ALWAYS present → match Python's `no attribute 'X'` wording instead. (hits: 1)
- 2026-09-06: `stage_listing` ran the policy resolver BEFORE the wizard save carrying the FB-mapped / caller condition → the resolver read the stale condition (Codex HIGH) → any server-side "derive from field X" call goes AFTER the save that writes X; assert call order in the test (`idx_save < idx_apply`), not just presence. (hits: 1)
- 2026-09-06: Dimension regex compiled without `re.I` and with an optional unit group → `400 x 300 x 200 mm` matched as unit-less (mm not in the group) and became inches; `CM` was ignored → for free-text unit parsing use complete case-insensitive tokens, convert per axis, and reject a trailing unknown word with a warning instead of defaulting. (hits: 1)
- 2026-09-06: Rebuilt `state["package"]` from the template read and replaced the server's dict → dropped `package_type` and the server's legacy-attribute fallback values, and let the description overwrite them → when the server returns a canonical block, it is the baseline: merge on top of it, consult it before any client-side fallback. (hits: 1)
