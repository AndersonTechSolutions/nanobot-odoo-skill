"""
Smart Action Handler — fuzzy, natural-language-friendly operations.

This is the *key* module for nanobot integration. It translates
high-level, imprecise commands ("create a quotation for Rocky with
5 Rocks") into the precise multi-step Odoo workflows:
  1. Find-or-create the partner "Rocky"
  2. Find-or-create each product ("Rock")
  3. Build the quotation with resolved IDs

All smart actions are resilient: they search first, create only
when necessary, and provide clear feedback about what was found
vs. what was created.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .client import OdooClient
from .models.partner import PartnerOps
from .models.sale_order import SaleOrderOps
from .models.invoice import InvoiceOps
from .models.inventory import InventoryOps
from .models.crm import CRMOps
from .models.purchase import PurchaseOrderOps
from .models.project import ProjectOps
from .models.hr import HROps
from .models.calendar_ops import CalendarOps
from .models.todo_matrix import TodoMatrixOps

logger = logging.getLogger("odoo_skill")


# Tokenizer for warehouse location phrases. Splits on whitespace and the
# separators that appear inside complete_name / name values in Odoo.
_LOC_TOKEN_RE = re.compile(r"[\s/,\-_]+")

# Aliases for phrases a human might say that the tokenizer alone can't
# reach. Keep this tight — add only when a phrase genuinely can't be
# resolved by the other passes. Keys are lowercased.
_LOCATION_ALIASES: dict[str, str] = {
    "pick station": "Shipping Station",
    "packing station": "Shipping Station",
}

# Regex-based rewrites for vocab that the tokenizer alone can't reach.
# Applied before tokenisation. Keep tight — every entry here is a
# promise that "humans say X, Odoo stores Y."
_LOCATION_REWRITES: list[tuple[re.Pattern, str]] = [
    # "metro rack 09 B"  → "MR09 B"  ;  "metro rack 9" → "MR9"
    # We keep the trailing "B" so the tokeniser picks up "B" and
    # narrows down to MR09-B rather than any MR09*.
    (re.compile(r"(?i)\bmetro\s*rack\s*(\d+)\b"),
     lambda m: f"MR{int(m.group(1)):02d}"),
    (re.compile(r"(?i)\bmr\s+(\d+)\b"),
     lambda m: f"MR{int(m.group(1)):02d}"),
    # "rolling shelf"     → "RLLNGSHELF"
    (re.compile(r"(?i)\brolling\s*shelf\b"), "RLLNGSHELF"),
]


def _apply_location_rewrites(phrase: str) -> str:
    out = phrase
    for pattern, repl in _LOCATION_REWRITES:
        out = pattern.sub(repl, out)
    return out


# Learned-vocab file. Populated at runtime by `odoo.py learn-location`,
# which Andy invokes after Ian confirms a new alias on Telegram.
# Format:
#   {
#     "aliases": { "<human phrase lowercased>": "<canonical Odoo name>", ... },
#     "version": 1
#   }
# Tracked in git so you can audit what vocab Andy has been taught and roll
# entries back manually if needed.
_LOCATION_VOCAB_PATH = (
    Path(__file__).resolve().parent.parent / "location_vocab.json"
)


def _load_learned_aliases() -> dict[str, str]:
    """Load the Ian-approved learned alias map from disk.

    Returns an empty dict if the file doesn't exist or is malformed —
    learned vocab is always additive, never required. Corrupt files
    log a warning so a human notices.
    """
    try:
        raw = _LOCATION_VOCAB_PATH.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
        aliases = data.get("aliases") or {}
        if not isinstance(aliases, dict):
            raise ValueError("aliases is not a dict")
        return {str(k).lower(): str(v) for k, v in aliases.items()}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Could not load learned location vocab from %s: %s — "
            "falling back to baked aliases only.",
            _LOCATION_VOCAB_PATH, exc,
        )
        return {}


def _save_learned_aliases(aliases: dict[str, str]) -> None:
    """Write the learned alias map to disk atomically."""
    payload = {"version": 1, "aliases": dict(sorted(aliases.items()))}
    tmp = _LOCATION_VOCAB_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(_LOCATION_VOCAB_PATH)


class SmartActionHandler:
    """Handles fuzzy, natural-language-style Odoo operations.

    Wraps the lower-level Ops classes and adds find-or-create logic,
    name-based lookups, and resilient multi-step workflows.

    Args:
        client: An authenticated :class:`OdooClient` instance.

    Example::

        smart = SmartActionHandler(client)
        result = smart.smart_create_quotation(
            customer_name="Rocky",
            product_lines=[{"name": "Rock", "quantity": 5}],
        )
    """

    def __init__(self, client: OdooClient) -> None:
        self.client = client
        self.partners = PartnerOps(client)
        self.sales = SaleOrderOps(client)
        self.invoices = InvoiceOps(client)
        self.inventory = InventoryOps(client)
        self.crm = CRMOps(client)
        self.purchase = PurchaseOrderOps(client)
        self.projects = ProjectOps(client)
        self.hr = HROps(client)
        self.calendar = CalendarOps(client)
        self.todo_matrix = TodoMatrixOps(client)

        # Learned aliases take precedence over baked-in ones so Ian can
        # override a bad built-in without editing code.
        self._location_aliases: dict[str, str] = {
            **_LOCATION_ALIASES,
            **_load_learned_aliases(),
        }

    # ── Location resolution ─────────────────────────────────────────
    #
    # Odoo's stock.location records use paths like "AT_WH/WH Stock/02-02-05".
    # Humans phrase locations every way except the canonical form. The
    # resolver normalises a human phrase to an internal location id so
    # task creation can tag warehouse spots without hand-coded mappings.

    @staticmethod
    def _tokenize_location(phrase: str) -> list[str]:
        """Split a location phrase into matchable tokens."""
        return [t for t in _LOC_TOKEN_RE.split(phrase.strip()) if t]

    def _search_locations(self, tokens: list[str]) -> list[dict]:
        """AND-of-ilike search on stock.location.complete_name for tokens."""
        if not tokens:
            return []
        domain: list = [["usage", "=", "internal"]]
        for tok in tokens:
            domain.append(["complete_name", "ilike", tok])
        return self.client.search_read(
            "stock.location",
            domain,
            fields=["id", "name", "complete_name"],
            limit=20,
        )

    def _resolve_location_id(self, location_name: str) -> int:
        """Resolve an internal stock.location from a human phrase.

        Strategy (first hit wins):
          1. Alias lookup — swap known phrasings ("pick station")
             to their canonical stored name.
          2. Tokenised AND-of-ilike on ``complete_name``.
          3. Compact-form fallback — strip all separators from the
             phrase and ilike against ``name`` ("photostudio" →
             ``PhotoStudio``).
          4. Zero-pad fallback — strip leading zeros from numeric
             tokens and retry the AND-of-ilike.
          5. Exact-match preference — if any candidate's ``name`` or
             ``complete_name`` matches the phrase case-insensitively,
             prefer it. Otherwise, when multiple candidates tie on
             ``complete_name`` (true duplicates in Odoo like the
             ``02-02-03`` pair), log a warning naming all ids and
             return the lowest id for stability.

        Args:
            location_name: Human phrase, e.g. ``"02-02-05"``,
                ``"wh stock 02-02-05"``, ``"photo studio"``,
                ``"metro rack 09 B"``, ``"rolling shelf B07"``.

        Returns:
            The internal ``stock.location`` id.

        Raises:
            ValueError: If no internal location matches. The message
                names the unresolved phrase and points to the
                ``list-locations`` CLI for disambiguation.
        """
        if not location_name or not location_name.strip():
            raise ValueError("Empty location phrase")

        original = location_name.strip()
        lookup = self._location_aliases.get(original.lower(), original)
        lookup = _apply_location_rewrites(lookup)

        # Pass 1 — tokenised AND-of-ilike
        tokens = self._tokenize_location(lookup)
        candidates = self._search_locations(tokens)

        # Pass 2 — compact form against name
        if not candidates:
            compact = re.sub(r"[\s/,\-_]+", "", lookup).lower()
            if compact:
                candidates = self.client.search_read(
                    "stock.location",
                    [["usage", "=", "internal"], ["name", "ilike", compact]],
                    fields=["id", "name", "complete_name"],
                    limit=20,
                )

        # Pass 3 — zero-pad-normalised tokens
        if not candidates:
            normalised = [
                str(int(t)) if t.isdigit() else t
                for t in tokens
            ]
            if normalised != tokens:
                candidates = self._search_locations(normalised)

        if not candidates:
            raise ValueError(
                f"Could not resolve location '{original}'. No internal "
                "stock.location matched. Try: python3 skills/odoo/odoo.py "
                f"list-locations --search '{tokens[0] if tokens else original}'"
            )

        target = original.lower()
        exact = [
            c for c in candidates
            if (c.get("name") or "").lower() == target
            or (c.get("complete_name") or "").lower() == target
        ]
        if exact:
            chosen = exact
        else:
            chosen = candidates

        # Detect duplicates on complete_name among chosen candidates
        by_name: dict[str, list[dict]] = {}
        for c in chosen:
            by_name.setdefault(c.get("complete_name") or "", []).append(c)

        best_name = min(by_name.keys(), key=lambda n: (0 if n.lower() == target else 1, n))
        bucket = by_name[best_name]
        if len(bucket) > 1:
            ids = sorted(c["id"] for c in bucket)
            logger.warning(
                "Location '%s' has duplicate stock.location records at %r: ids %s. "
                "Returning lowest id %d. Consider deactivating the dupe in Odoo.",
                original, best_name, ids, ids[0],
            )
            return ids[0]
        return bucket[0]["id"]

    def learn_location(self, phrase: str, target: str) -> dict:
        """Teach Andy a new location alias after Ian's confirmation.

        Adds ``phrase → target`` to the learned-aliases file at
        ``skills/odoo/location_vocab.json`` so future resolutions of
        ``phrase`` short-circuit to the intended location without
        needing a redeploy.

        Called by Andy ONLY after Ian confirms the mapping on Telegram.
        Both arguments are required because Andy must state what it
        heard (the phrase) and what it intends to bind it to (the
        target location name or complete_name).

        Safety:
          - The target must itself resolve to a real internal
            ``stock.location`` via the existing resolver. If it
            doesn't, the learn call raises ``ValueError`` and no
            file is written.
          - Existing phrases are overwritten; a WARNING is logged
            naming the prior target so there's a trail.

        Args:
            phrase: The human phrasing that failed to resolve
                (e.g. ``"kanban wall"``).
            target: An Odoo location name or complete_name that the
                resolver already accepts (e.g. ``"KANBAN-01"`` or
                ``"AT_WH/WH Stock/KANBAN-01"``).

        Returns:
            Dict with ``phrase``, ``target``, ``resolved_id``,
            ``resolved_complete_name``, ``previous_target`` (or None),
            and ``vocab_path`` confirming where it was written.

        Raises:
            ValueError: If ``phrase`` or ``target`` is empty, or if
                ``target`` cannot be resolved.
        """
        if not phrase or not phrase.strip():
            raise ValueError("learn_location: phrase must be non-empty")
        if not target or not target.strip():
            raise ValueError("learn_location: target must be non-empty")

        key = phrase.strip().lower()

        # Validate target resolves to a real internal location. We call
        # the resolver directly — if it fails, we do NOT write vocab.
        resolved_id = self._resolve_location_id(target)
        resolved_records = self.client.search_read(
            "stock.location",
            [["id", "=", resolved_id]],
            fields=["id", "name", "complete_name"],
            limit=1,
        )
        resolved = resolved_records[0] if resolved_records else {
            "id": resolved_id, "name": target, "complete_name": target,
        }

        # Merge and persist. Preserve the baked-in aliases in memory
        # but only write the learned subset to disk — baked aliases
        # live in code.
        learned = _load_learned_aliases()
        previous_target = learned.get(key)
        if previous_target is not None and previous_target != target:
            logger.warning(
                "Overwriting learned location alias %r: %r → %r",
                key, previous_target, target,
            )
        learned[key] = target
        _save_learned_aliases(learned)

        # Update this instance's live map too, so the new alias works
        # immediately without restarting Andy.
        self._location_aliases[key] = target

        logger.info(
            "Learned location alias: %r → %r (resolves to id=%d complete_name=%r)",
            key, target, resolved["id"], resolved.get("complete_name"),
        )

        return {
            "phrase": key,
            "target": target,
            "resolved_id": resolved["id"],
            "resolved_complete_name": resolved.get("complete_name"),
            "previous_target": previous_target,
            "vocab_path": str(_LOCATION_VOCAB_PATH),
        }

    # ── Find-or-Create primitives ────────────────────────────────────

    def find_or_create_partner(
        self,
        name: str,
        is_company: bool = True,
        supplier: bool = False,
        **defaults: Any,
    ) -> dict:
        """Search for a partner by name. Create if not found.

        Uses case-insensitive ``ilike`` matching. If multiple matches,
        returns the best match (exact name match preferred).

        Args:
            name: Partner/company name to search for.
            is_company: Whether to create as a company (if creating).
            supplier: If ``True``, set ``supplier_rank=1`` on creation.
            **defaults: Additional fields to set when creating.

        Returns:
            Dict with ``partner`` (record), ``created`` (bool), and
            ``matched`` (list of all matches found).
        """
        # Search existing partners
        results = self.client.search_read(
            "res.partner",
            [["name", "ilike", name], ["active", "=", True]],
            fields=["id", "name", "email", "phone", "is_company",
                    "customer_rank", "supplier_rank"],
            limit=10,
        )

        if results:
            # Prefer exact name match (case-insensitive)
            exact = [r for r in results if r["name"].lower() == name.lower()]
            best = exact[0] if exact else results[0]
            logger.info("Found existing partner %r (id=%d)", best["name"], best["id"])
            return {
                "partner": best,
                "created": False,
                "matched": results,
            }

        # Not found — create
        create_vals: dict[str, Any] = {
            "name": name,
            "is_company": is_company,
            "customer_rank": 1,
        }
        if supplier:
            create_vals["supplier_rank"] = 1
        create_vals.update(defaults)

        partner_id = self.client.create("res.partner", create_vals)
        partner = self.client.read(
            "res.partner", partner_id,
            fields=["id", "name", "email", "phone", "is_company"],
        )[0]
        logger.info("Created new partner %r (id=%d)", name, partner_id)

        return {
            "partner": partner,
            "created": True,
            "matched": [],
        }

    def find_or_create_product(
        self,
        name: str,
        **defaults: Any,
    ) -> dict:
        """Search for a product by name or internal reference. Create if not found.

        Args:
            name: Product name or SKU to search for.
            **defaults: Additional fields to set when creating
                (e.g. ``list_price``, ``type``).

        Returns:
            Dict with ``product`` (record), ``created`` (bool), and
            ``matched`` (list of all matches found).
        """
        # Search by name or internal reference (SKU)
        results = self.client.search_read(
            "product.product",
            ["|", ["name", "ilike", name], ["default_code", "ilike", name]],
            fields=["id", "name", "default_code", "list_price", "type"],
            limit=10,
        )

        if results:
            # Prefer exact name match
            exact = [r for r in results if r["name"].lower() == name.lower()]
            best = exact[0] if exact else results[0]
            logger.info("Found existing product %r (id=%d)", best["name"], best["id"])
            return {
                "product": best,
                "created": False,
                "matched": results,
            }

        # Not found — create a basic product
        create_vals: dict[str, Any] = {
            "name": name,
            "type": defaults.pop("type", "consu"),
            "list_price": defaults.pop("list_price", 0.0),
        }
        create_vals.update(defaults)

        product_id = self.client.create("product.product", create_vals)
        product = self.client.read(
            "product.product", product_id,
            fields=["id", "name", "default_code", "list_price", "type"],
        )[0]
        logger.info("Created new product %r (id=%d)", name, product_id)

        return {
            "product": product,
            "created": True,
            "matched": [],
        }

    def _find_or_create_project(self, name: str, **defaults: Any) -> dict:
        """Search for a project by name. Create if not found.

        Args:
            name: Project name.
            **defaults: Additional fields for creation.

        Returns:
            Dict with ``project`` (record) and ``created`` (bool).
        """
        results = self.client.search_read(
            "project.project",
            [["name", "ilike", name], ["active", "=", True]],
            fields=["id", "name", "user_id", "partner_id"],
            limit=5,
        )

        if results:
            exact = [r for r in results if r["name"].lower() == name.lower()]
            best = exact[0] if exact else results[0]
            return {"project": best, "created": False}

        project = self.projects.create_project(name, **defaults)
        return {"project": project, "created": True}

    # ── Smart composite actions ──────────────────────────────────────

    def smart_create_quotation(
        self,
        customer_name: str,
        product_lines: list[dict],
        notes: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Create a quotation from names (not IDs).

        Resolves customer and products by name, creating them if needed,
        then builds the sales quotation.

        Args:
            customer_name: Customer/company name.
            product_lines: List of dicts with ``name`` (product name),
                and optionally ``quantity``, ``price_unit``, ``discount``.
            notes: Optional order notes.
            **kwargs: Additional ``sale.order`` field values.

        Returns:
            Dict with ``order`` (the created quotation), ``customer`` info,
            and ``products`` info (showing what was found vs created).

        Example::

            result = smart.smart_create_quotation(
                customer_name="Rocky",
                product_lines=[
                    {"name": "Rock", "quantity": 5},
                    {"name": "Pebble", "quantity": 20, "price_unit": 1.50},
                ],
            )
        """
        # Step 1: Resolve customer
        customer_result = self.find_or_create_partner(customer_name)
        partner_id = customer_result["partner"]["id"]

        # Step 2: Resolve products and build order lines
        resolved_lines = []
        products_info = []
        for line in product_lines:
            product_name = line.get("name", line.get("product_name", ""))
            if not product_name and "product_id" in line:
                # Already have a product ID — use directly
                resolved_lines.append(line)
                products_info.append({"product_id": line["product_id"], "created": False})
                continue

            price_default = {}
            if "price_unit" in line:
                price_default["list_price"] = line["price_unit"]

            product_result = self.find_or_create_product(product_name, **price_default)
            product = product_result["product"]

            order_line: dict[str, Any] = {
                "product_id": product["id"],
                "quantity": line.get("quantity", line.get("qty", 1)),
            }
            if "price_unit" in line:
                order_line["price_unit"] = line["price_unit"]
            if "discount" in line:
                order_line["discount"] = line["discount"]

            resolved_lines.append(order_line)
            products_info.append({
                "product": product,
                "created": product_result["created"],
            })

        # Step 3: Create the quotation
        order = self.sales.create_quotation(
            partner_id=partner_id,
            lines=resolved_lines,
            notes=notes,
            **kwargs,
        )

        return {
            "order": order,
            "customer": customer_result,
            "products": products_info,
            "summary": (
                f"Quotation {order.get('name', '')} created for "
                f"{customer_result['partner']['name']} with "
                f"{len(resolved_lines)} line(s)"
            ),
        }

    def smart_create_invoice(
        self,
        customer_name: str,
        lines: list[dict],
        invoice_date: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Create an invoice from names (not IDs).

        Args:
            customer_name: Customer/company name.
            lines: List of dicts with ``name`` or ``description``,
                ``price_unit``, and optionally ``quantity``, ``product_name``.
            invoice_date: Invoice date as ``YYYY-MM-DD``.
            **kwargs: Additional ``account.move`` field values.

        Returns:
            Dict with ``invoice``, ``customer`` info, and ``products`` info.
        """
        # Resolve customer
        customer_result = self.find_or_create_partner(customer_name)
        partner_id = customer_result["partner"]["id"]

        # Resolve products in lines (if product names are provided)
        resolved_lines = []
        products_info = []
        for line in lines:
            il: dict[str, Any] = {
                "price_unit": line.get("price_unit", 0),
                "quantity": line.get("quantity", 1),
                "name": line.get("description", line.get("name", "")),
            }

            product_name = line.get("product_name", line.get("product", ""))
            if product_name:
                product_result = self.find_or_create_product(product_name)
                il["product_id"] = product_result["product"]["id"]
                products_info.append({
                    "product": product_result["product"],
                    "created": product_result["created"],
                })
            elif "product_id" in line:
                il["product_id"] = line["product_id"]

            resolved_lines.append(il)

        # Create the invoice
        invoice = self.invoices.create_invoice(
            partner_id=partner_id,
            lines=resolved_lines,
            invoice_date=invoice_date,
            **kwargs,
        )

        return {
            "invoice": invoice,
            "customer": customer_result,
            "products": products_info,
            "summary": (
                f"Invoice {invoice.get('name', '')} created for "
                f"{customer_result['partner']['name']}"
            ),
        }

    def smart_create_lead(
        self,
        name: str,
        contact_name: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        expected_revenue: Optional[float] = None,
        **kwargs: Any,
    ) -> dict:
        """Create a CRM lead with optional partner linking.

        If ``contact_name`` is provided, attempts to find and link
        an existing partner.

        Args:
            name: Lead title.
            contact_name: Contact person name.
            email: Contact email.
            phone: Contact phone.
            expected_revenue: Estimated deal value.
            **kwargs: Additional ``crm.lead`` field values.

        Returns:
            Dict with ``lead`` and optionally ``partner`` info.
        """
        extra: dict[str, Any] = dict(kwargs)

        # Try to link to existing partner if contact name is given
        partner_info = None
        if contact_name:
            partner_result = self.find_or_create_partner(
                contact_name, is_company=False,
            )
            extra["partner_id"] = partner_result["partner"]["id"]
            partner_info = partner_result

        lead = self.crm.create_lead(
            name=name,
            contact_name=contact_name,
            email=email,
            phone=phone,
            expected_revenue=expected_revenue,
            **extra,
        )

        return {
            "lead": lead,
            "partner": partner_info,
            "summary": f"Lead '{name}' created (id={lead.get('id', '?')})",
        }

    def smart_create_purchase(
        self,
        vendor_name: str,
        product_lines: list[dict],
        date_planned: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Create a purchase order from names (not IDs).

        Args:
            vendor_name: Vendor/supplier name.
            product_lines: List of dicts with ``name`` (product name),
                and optionally ``quantity``, ``price_unit``.
            date_planned: Expected receipt date.
            **kwargs: Additional ``purchase.order`` field values.

        Returns:
            Dict with ``purchase_order``, ``vendor`` info, and ``products`` info.
        """
        # Resolve vendor
        vendor_result = self.find_or_create_partner(
            vendor_name, supplier=True,
        )
        partner_id = vendor_result["partner"]["id"]

        # Ensure supplier_rank is set
        if not vendor_result["created"]:
            partner = vendor_result["partner"]
            if not partner.get("supplier_rank"):
                self.client.write("res.partner", partner_id, {"supplier_rank": 1})

        # Resolve products
        resolved_lines = []
        products_info = []
        for line in product_lines:
            product_name = line.get("name", line.get("product_name", ""))
            if product_name:
                product_result = self.find_or_create_product(product_name)
                product = product_result["product"]
                products_info.append({
                    "product": product,
                    "created": product_result["created"],
                })

                order_line: dict[str, Any] = {
                    "product_id": product["id"],
                    "quantity": line.get("quantity", line.get("qty", 1)),
                }
                if "price_unit" in line:
                    order_line["price_unit"] = line["price_unit"]
                resolved_lines.append(order_line)

        # Create the purchase order
        po = self.purchase.create_purchase_order(
            partner_id=partner_id,
            lines=resolved_lines,
            date_planned=date_planned,
            **kwargs,
        )

        return {
            "purchase_order": po,
            "vendor": vendor_result,
            "products": products_info,
            "summary": (
                f"Purchase Order {po.get('name', '')} created for "
                f"vendor {vendor_result['partner']['name']}"
            ),
        }

    def smart_create_task(
        self,
        project_name: str,
        task_name: str,
        description: Optional[str] = None,
        date_deadline: Optional[str] = None,
        assignee_name: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Create a task in a project, resolving project by name.

        Args:
            project_name: Name of the project (found or created).
            task_name: Task title.
            description: Task description.
            date_deadline: Due date as ``YYYY-MM-DD``.
            assignee_name: Name of the user to assign (searches ``res.users``).
            **kwargs: Additional ``project.task`` field values.

        Returns:
            Dict with ``task``, ``project`` info, and optionally ``assignee``.
        """
        # Resolve project
        project_result = self._find_or_create_project(project_name)
        project_id = project_result["project"]["id"]

        # Resolve assignee if provided
        assignee_info = None
        user_ids = None
        if assignee_name:
            users = self.client.search_read(
                "res.users",
                [["name", "ilike", assignee_name], ["active", "=", True]],
                fields=["id", "name"],
                limit=5,
            )
            if users:
                user_ids = [users[0]["id"]]
                assignee_info = users[0]

        task = self.projects.create_task(
            project_id=project_id,
            name=task_name,
            user_ids=user_ids,
            description=description,
            date_deadline=date_deadline,
            **kwargs,
        )

        return {
            "task": task,
            "project": project_result,
            "assignee": assignee_info,
            "summary": (
                f"Task '{task_name}' created in project "
                f"'{project_result['project']['name']}'"
            ),
        }

    def smart_create_employee(
        self,
        name: str,
        job_title: Optional[str] = None,
        department_name: Optional[str] = None,
        work_email: Optional[str] = None,
        work_phone: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Create an employee, resolving department by name.

        Args:
            name: Employee's full name.
            job_title: Job title/position.
            department_name: Department name (found or created).
            work_email: Work email address.
            work_phone: Work phone number.
            **kwargs: Additional ``hr.employee`` field values.

        Returns:
            Dict with ``employee`` and optionally ``department`` info.
        """
        # Check if employee already exists by name
        existing = self.client.search_read(
            "hr.employee",
            [["name", "ilike", name], ["active", "=", True]],
            fields=["id", "name", "job_title", "department_id"],
            limit=5,
        )
        if existing:
            exact = [e for e in existing if e["name"].lower() == name.lower()]
            if exact:
                return {
                    "employee": exact[0],
                    "created": False,
                    "summary": f"Employee '{name}' already exists (id={exact[0]['id']})",
                }

        # Resolve department if provided
        department_info = None
        department_id = kwargs.pop("department_id", None)
        if department_name and not department_id:
            depts = self.client.search_read(
                "hr.department",
                [["name", "ilike", department_name]],
                fields=["id", "name"],
                limit=5,
            )
            if depts:
                department_id = depts[0]["id"]
                department_info = {"department": depts[0], "created": False}
            else:
                dept = self.hr.create_department(department_name)
                department_id = dept["id"]
                department_info = {"department": dept, "created": True}

        employee = self.hr.create_employee(
            name=name,
            job_title=job_title,
            department_id=department_id,
            work_email=work_email,
            work_phone=work_phone,
            **kwargs,
        )

        return {
            "employee": employee,
            "department": department_info,
            "created": True,
            "summary": f"Employee '{name}' created (id={employee.get('id', '?')})",
        }

    def smart_create_event(
        self,
        name: str,
        start: str,
        end: Optional[str] = None,
        location: Optional[str] = None,
        attendee_names: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict:
        """Create a calendar event, resolving attendees by name.

        Args:
            name: Event title.
            start: Start datetime as ``YYYY-MM-DD HH:MM:SS`` or
                ``YYYY-MM-DD`` for all-day events.
            end: End datetime. Defaults to 1 hour after start.
            location: Event location.
            attendee_names: List of partner/contact names to invite.
            **kwargs: Additional ``calendar.event`` field values.

        Returns:
            Dict with ``event`` and ``attendees`` info.
        """
        # Resolve attendees by name
        partner_ids = []
        attendees_info = []
        if attendee_names:
            for att_name in attendee_names:
                result = self.find_or_create_partner(att_name, is_company=False)
                partner_ids.append(result["partner"]["id"])
                attendees_info.append(result)

        # Detect all-day event
        allday = len(start) == 10  # Just a date, no time

        event = self.calendar.create_event(
            name=name,
            start=start,
            stop=end,
            allday=allday,
            location=location,
            partner_ids=partner_ids if partner_ids else None,
            **kwargs,
        )

        return {
            "event": event,
            "attendees": attendees_info,
            "summary": (
                f"Event '{name}' created at {start}"
                + (f" with {len(attendees_info)} attendee(s)" if attendees_info else "")
            ),
        }

    # ── To-Do Priority Matrix smart actions ───────────────────────────

    def smart_create_todo(
        self,
        task_name: str,
        employee_name: Optional[str] = None,
        is_urgent: bool = False,
        is_important: bool = False,
        description: Optional[str] = None,
        deadline: Optional[str] = None,
        estimated_time: Optional[float] = None,
        location_name: Optional[str] = None,
        employee_names: Optional[list] = None,
        primary_employee_name: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Create a to-do task in the priority matrix, resolving employees by name.

        Supports shared tasks: pass multiple names via ``employee_names`` and
        optionally designate a primary owner via ``primary_employee_name``.
        The legacy ``employee_name`` (single string) is still accepted.

        Args:
            task_name: Task title.
            employee_name: Single employee name (legacy, fuzzy matched).
                Ignored when ``employee_names`` is provided.
            employee_names: List of employee names for a shared task.
                At least one name is required (either this or
                ``employee_name``). The first name is the primary unless
                ``primary_employee_name`` overrides it.
            primary_employee_name: Name of the primary assignee. Must be
                one of the names in ``employee_names``. Defaults to the
                first entry.
            is_urgent: Whether the task is urgent.
            is_important: Whether the task is important.
            description: Task description.
            deadline: Due date as ``YYYY-MM-DD``.
            estimated_time: Estimated hours.
            location_name: Optional warehouse location phrase. Fails loud
                (``ValueError``) if unresolved.
            **kwargs: Additional ``employee.todo.task`` field values.

        Returns:
            Dict with ``task``, ``employees``, ``primary_employee``,
            ``quadrant``, and ``summary``.

        Example (shared task)::

            result = smart.smart_create_todo(
                task_name="Build foam corners",
                employee_names=["Martin", "Jasmine"],
                primary_employee_name="Martin",
                is_urgent=True,
                is_important=True,
                deadline="2026-05-08",
            )
        """
        # Normalise to a list of names
        names: list[str] = []
        if employee_names:
            names = list(employee_names)
        elif employee_name:
            names = [employee_name]
        if not names:
            raise ValueError(
                "smart_create_todo requires employee_name or employee_names."
            )

        def _resolve(name: str) -> dict:
            results = self.client.search_read(
                "hr.employee",
                [["name", "ilike", name], ["active", "=", True]],
                fields=["id", "name", "job_title", "department_id"],
                limit=5,
            )
            if not results:
                raise ValueError(f"No employee found matching '{name}'")
            exact = [e for e in results if e["name"].lower() == name.lower()]
            return exact[0] if exact else results[0]

        resolved = [_resolve(n) for n in names]
        employee_ids = [e["id"] for e in resolved]

        # Determine primary
        primary_emp = resolved[0]
        if primary_employee_name:
            pname = primary_employee_name.lower()
            match = next((e for e in resolved if e["name"].lower() == pname), None)
            if match is None:
                raise ValueError(
                    f"primary_employee_name '{primary_employee_name}' is not in "
                    f"the resolved assignee list: {[e['name'] for e in resolved]}"
                )
            primary_emp = match

        if location_name:
            kwargs["location_id"] = self._resolve_location_id(location_name)

        category_names = kwargs.pop("category_names", None)
        if category_names:
            kwargs["category_ids"] = self.todo_matrix.resolve_category_ids(category_names)

        task = self.todo_matrix.create_task(
            name=task_name,
            employee_ids=employee_ids,
            primary_employee_id=primary_emp["id"],
            is_urgent=is_urgent,
            is_important=is_important,
            description=description,
            deadline=deadline,
            estimated_time=estimated_time,
            **kwargs,
        )

        quadrant_labels = {
            "do": "Do First (urgent + important)",
            "schedule": "Schedule (important, not urgent)",
            "delegate": "Delegate (urgent, not important)",
            "eliminate": "Eliminate (neither)",
        }
        quadrant = task.get("eisenhower_quadrant", "eliminate")
        assignee_names = ", ".join(e["name"] for e in resolved)

        return {
            "task": task,
            "employee": primary_emp,
            "employees": resolved,
            "primary_employee": primary_emp,
            "quadrant": quadrant,
            "summary": (
                f"To-do '{task_name}' created for {assignee_names} "
                f"(primary: {primary_emp['name']}) "
                f"→ {quadrant_labels.get(quadrant, quadrant)}"
            ),
        }

    def smart_get_matrix(
        self,
        employee_name: str,
    ) -> dict:
        """Get an employee's Eisenhower priority matrix.

        Args:
            employee_name: Employee name (fuzzy matched).

        Returns:
            Dict with ``matrix`` (quadrant data), ``employee``, and ``summary``.
        """
        employees = self.client.search_read(
            "hr.employee",
            [["name", "ilike", employee_name], ["active", "=", True]],
            fields=["id", "name"],
            limit=5,
        )

        if not employees:
            raise ValueError(f"No employee found matching '{employee_name}'")

        exact = [e for e in employees if e["name"].lower() == employee_name.lower()]
        employee = exact[0] if exact else employees[0]

        matrix = self.todo_matrix.get_matrix(employee["id"])

        return {
            "matrix": matrix,
            "employee": employee,
            "summary": (
                f"Priority Matrix for {employee['name']}: "
                f"{matrix['summary']['do']} Do First, "
                f"{matrix['summary']['schedule']} Schedule, "
                f"{matrix['summary']['delegate']} Delegate, "
                f"{matrix['summary']['eliminate']} Eliminate "
                f"({matrix['summary']['total']} total)"
            ),
        }

    def smart_get_team_workload(self) -> dict:
        """Get team workload dashboard data.

        Returns:
            Dict with ``workload`` data and ``summary``.
        """
        workload = self.todo_matrix.get_team_workload()
        totals = workload.get("team_totals", {})

        return {
            "workload": workload,
            "summary": (
                f"Team Workload: {totals.get('employee_count', 0)} members, "
                f"{totals.get('total_active', 0)} active tasks, "
                f"{totals.get('total_overdue', 0)} overdue, "
                f"{totals.get('total_estimated_hours', 0):.1f}h estimated"
            ),
        }
