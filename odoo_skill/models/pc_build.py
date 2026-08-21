"""
PC build operations for the ``pc_configurator`` module (Odoo 17 ``pc.build``
/ ``pc.build.line``).

A build is a spec sheet: pick components by type (cpu, motherboard, ram, gpu,
storage, case, psu), the module checks them against its compatibility rules
and estimates power draw, and the result becomes a quotation, a manufacturing
order, or a saved catalog product. Builds run ``draft -> validated -> done``,
either from ``scratch`` or as a ``base_upgrade`` on an existing machine.

Three things shape this class:

* **Compatibility is advisory, not enforced.** ``compatibility_status`` is
  computed (``ok`` / ``error`` / ``incomplete``) with the detail in
  ``compatibility_messages``, but nothing stops a caller quoting an
  incompatible build. So :meth:`PcBuildOps.create_quotation` and
  :meth:`create_build_order` check it first and refuse on ``error`` unless
  explicitly overridden — the module's own UI shows the warning to a human
  who can weigh it, and an unattended agent has no such reader.

* **``has_speculative_parts`` is** ``searchable: False``. A speculative part
  is one the build assumes but does not yet own; filtering on it server-side
  would silently return every build, so it goes client-side.

* **Upgrade lines carry a removal.** In ``base_upgrade`` mode a line with
  ``upgrade_action == 'replace'`` names the part coming *out* in
  ``removed_product_id``. Adding a replace line without it produces an
  upgrade that quietly double-counts the old component.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "partner_id", "state", "build_mode", "build_kind",
    "compatibility_status", "total_price", "est_power_draw",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "compatibility_messages", "base_product_id", "line_ids",
    "config_signature", "sale_order_id", "sale_line_id",
    "production_id", "manufactured_product_id", "create_date",
]

_LINE_FIELDS = [
    "id", "build_id", "component_type", "product_id", "qty",
    "price_unit", "price_subtotal", "upgrade_action", "removed_product_id",
]

#: ``pc.build.state`` values.
STATES = ["draft", "validated", "done"]

#: ``build_mode`` values.
BUILD_MODES = ["scratch", "base_upgrade"]

#: ``build_kind`` values.
BUILD_KINDS = ["one_off", "catalog"]

#: ``pc.build.line.component_type`` values.
COMPONENT_TYPES = [
    "cpu", "motherboard", "ram", "gpu", "storage", "case", "psu",
]

#: ``compatibility_status`` values.
COMPAT_STATES = ["ok", "error", "incomplete"]


class PcBuildOps(BaseOps):
    """Operations on ``pc.build`` and its component lines."""

    MODEL = "pc.build"
    MODULE = "pc_configurator"
    LINE_MODEL = "pc.build.line"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "id desc"

    ALLOWED_ACTIONS = frozenset({
        "action_create_quotation",
        "action_create_build_order",
        "action_apply_to_sale_line",
        "action_save_as_catalog",
        "action_new_build_from_this",
        "action_convert_speculative_parts",
    })

    # ── Reads ────────────────────────────────────────────────────────

    def draft_builds(self, limit: int = 50) -> list[dict]:
        """Builds still being specced."""
        return self.search([["state", "=", "draft"]], limit=limit)

    def incompatible_builds(self, limit: int = 50) -> list[dict]:
        """Builds whose components clash — the queue that blocks quoting."""
        return self.search(
            [["compatibility_status", "=", "error"], ["state", "!=", "done"]],
            limit=limit,
        )

    def incomplete_builds(self, limit: int = 50) -> list[dict]:
        """Builds missing required components."""
        return self.search(
            [["compatibility_status", "=", "incomplete"], ["state", "!=", "done"]],
            limit=limit,
        )

    def builds_for_customer(self, partner_id: int, limit: int = 50) -> list[dict]:
        """Every build specced for one customer."""
        return self.search([["partner_id", "=", partner_id]], limit=limit)

    def catalog_builds(self, limit: int = 50) -> list[dict]:
        """Saved catalog configurations, as opposed to one-off customer builds."""
        return self.search([["build_kind", "=", "catalog"]], limit=limit)

    def unquoted_builds(self, limit: int = 50) -> list[dict]:
        """Validated builds that never became a quotation."""
        return self.search(
            [["state", "=", "validated"], ["sale_order_id", "=", False]],
            limit=limit,
        )

    def speculative_builds(self, limit: int = 50) -> list[dict]:
        """Builds relying on parts not actually in stock.

        ``has_speculative_parts`` is ``searchable: False``, so this filters
        client-side over the open set — a domain on it would return every
        open build instead.
        """
        return self.search_computed(
            [["state", "!=", "done"]],
            lambda r: bool(r.get("has_speculative_parts")),
            limit=limit, extra_fields=["has_speculative_parts"],
        )

    def get_lines(self, build_id: int) -> list[dict]:
        """Component lines of a build, in component-type order."""
        self._require()
        return self.client.search_read(
            self.LINE_MODEL, [["build_id", "=", build_id]],
            fields=_LINE_FIELDS, order="component_type, id",
        )

    def build_detail(self, build_id: int) -> dict:
        """A build with its lines, compatibility verdict, and what is missing."""
        record = self.get(build_id)
        lines = self.client.search_read(
            self.LINE_MODEL, [["build_id", "=", build_id]],
            fields=_LINE_FIELDS, order="component_type, id",
        )
        present = {line["component_type"] for line in lines}
        missing = [t for t in COMPONENT_TYPES if t not in present]
        status = record.get("compatibility_status")
        return {
            "summary": (
                f"Build {record.get('name') or build_id}: {len(lines)} parts, "
                f"{record.get('total_price')} total, ~{record.get('est_power_draw')}W, "
                f"compatibility {status}"
                + (f"; missing {', '.join(missing)}" if missing else "")
            ),
            "compatibility_status": status,
            "compatibility_messages": record.get("compatibility_messages"),
            "missing_types": missing,
            "lines": lines,
            "build": record,
        }

    # ── Writes ───────────────────────────────────────────────────────

    def create_build(
        self,
        build_mode: str = "scratch",
        partner_id: Optional[int] = None,
        base_product_id: Optional[int] = None,
        build_kind: str = "one_off",
        name: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Start a build.

        Args:
            build_mode: ``scratch`` for a new machine, ``base_upgrade`` to
                start from an existing unit — which then requires
                *base_product_id*.
            partner_id: Customer the build is for.
            base_product_id: The unit being upgraded (``base_upgrade`` only).
            build_kind: ``one_off`` or ``catalog``.
            name: Optional label.
            **extra: Any other ``pc.build`` field.
        """
        if build_mode not in BUILD_MODES:
            raise ValueError(
                f"build_mode must be one of {BUILD_MODES}, got {build_mode!r}"
            )
        if build_kind not in BUILD_KINDS:
            raise ValueError(
                f"build_kind must be one of {BUILD_KINDS}, got {build_kind!r}"
            )
        if build_mode == "base_upgrade" and not base_product_id:
            raise ValueError(
                "base_upgrade builds need base_product_id — the unit being "
                "upgraded"
            )

        values: dict[str, Any] = {
            "build_mode": build_mode,
            "build_kind": build_kind,
        }
        if partner_id:
            values["partner_id"] = partner_id
        if base_product_id:
            values["base_product_id"] = base_product_id
        if name:
            values["name"] = name
        values.update(extra)

        record = self.create(values)
        return {
            "summary": (
                f"Build {record.get('name') or record['id']} started "
                f"({build_mode}, {build_kind})"
            ),
            "build": record,
        }

    def add_component(
        self,
        build_id: int,
        product_id: int,
        component_type: str,
        qty: int = 1,
        upgrade_action: Optional[str] = None,
        removed_product_id: Optional[int] = None,
    ) -> dict:
        """Add a component line to a build.

        Args:
            build_id: Build to add to.
            product_id: The component ``product.product``.
            component_type: One of :data:`COMPONENT_TYPES`.
            qty: Quantity (RAM and storage are the ones that are usually >1).
            upgrade_action: ``add`` or ``replace``, for ``base_upgrade`` builds.
            removed_product_id: The part coming out. Required when
                *upgrade_action* is ``replace`` — without it the upgrade
                double-counts the component being swapped.

        Returns:
            The build's refreshed compatibility verdict alongside the new line,
            since adding a part is exactly when that can change.
        """
        if component_type not in COMPONENT_TYPES:
            raise ValueError(
                f"component_type must be one of {COMPONENT_TYPES}, "
                f"got {component_type!r}"
            )
        if upgrade_action and upgrade_action not in ("add", "replace"):
            raise ValueError(
                f"upgrade_action must be 'add' or 'replace', got {upgrade_action!r}"
            )
        if upgrade_action == "replace" and not removed_product_id:
            raise ValueError(
                "a 'replace' line needs removed_product_id — the part coming "
                "out of the base unit"
            )

        self._require()
        values: dict[str, Any] = {
            "build_id": build_id,
            "product_id": product_id,
            "component_type": component_type,
            "qty": qty,
        }
        if upgrade_action:
            values["upgrade_action"] = upgrade_action
        if removed_product_id:
            values["removed_product_id"] = removed_product_id
        line_id = self.client.create(self.LINE_MODEL, values)

        detail = self.build_detail(build_id)
        return {
            "summary": (
                f"{component_type} added to build {build_id}. "
                f"Compatibility now {detail['compatibility_status']}."
            ),
            "line_id": line_id,
            "compatibility_status": detail["compatibility_status"],
            "compatibility_messages": detail["compatibility_messages"],
            "missing_types": detail["missing_types"],
        }

    def remove_component(self, line_id: int) -> dict:
        """Remove a component line from a build."""
        self._require()
        rows = self.client.read(self.LINE_MODEL, [line_id], fields=["build_id"])
        if not rows:
            raise ValueError(f"No pc.build.line with id {line_id}")
        build = rows[0]["build_id"]
        build_id = build[0] if isinstance(build, (list, tuple)) else build
        self.client.unlink(self.LINE_MODEL, [line_id])
        detail = self.build_detail(build_id)
        return {
            "summary": (
                f"Line {line_id} removed. Compatibility now "
                f"{detail['compatibility_status']}."
            ),
            "compatibility_status": detail["compatibility_status"],
            "missing_types": detail["missing_types"],
        }

    def _guard_compatibility(self, build_id: int, override: bool) -> Optional[dict]:
        """Refuse a downstream action on a build the module calls incompatible.

        Returns a refusal envelope, or ``None`` when it is safe to proceed.
        """
        detail = self.build_detail(build_id)
        status = detail["compatibility_status"]
        if status == "error" and not override:
            return {
                "summary": (
                    f"Refused — build {build_id} has compatibility errors: "
                    f"{detail['compatibility_messages'] or '(no detail)'}. "
                    "Fix the parts, or pass override=True to proceed anyway."
                ),
                "ok": False,
                "compatibility_status": status,
                "compatibility_messages": detail["compatibility_messages"],
            }
        return None

    def create_quotation(self, build_id: int, override: bool = False) -> dict:
        """Turn a build into a customer quotation.

        Blocked when the module reports compatibility errors, unless
        *override* is set — a quote for a machine that cannot be assembled is
        worse than no quote.
        """
        refusal = self._guard_compatibility(build_id, override)
        if refusal:
            return refusal
        return self.run_action(build_id, "action_create_quotation")

    def create_build_order(self, build_id: int, override: bool = False) -> dict:
        """Raise the manufacturing order that actually assembles the build."""
        refusal = self._guard_compatibility(build_id, override)
        if refusal:
            return refusal
        return self.run_action(build_id, "action_create_build_order")

    def save_as_catalog(self, build_id: int) -> dict:
        """Promote a one-off build to a reusable catalog configuration."""
        return self.run_action(build_id, "action_save_as_catalog")

    # ── Summary ──────────────────────────────────────────────────────

    def configurator_summary(self) -> dict:
        """Build pipeline plus the two queues that stop a build shipping."""
        counts = {s: self.count([["state", "=", s]]) for s in STATES}
        errors = self.count(
            [["compatibility_status", "=", "error"], ["state", "!=", "done"]]
        )
        incomplete = self.count(
            [["compatibility_status", "=", "incomplete"], ["state", "!=", "done"]]
        )
        unquoted = self.count(
            [["state", "=", "validated"], ["sale_order_id", "=", False]]
        )
        speculative = self.count_computed(
            [["state", "!=", "done"]],
            lambda r: bool(r.get("has_speculative_parts")),
            extra_fields=["has_speculative_parts"],
        )
        return {
            "summary": (
                f"PC builds: {counts['draft']} draft, {counts['validated']} validated, "
                f"{errors} incompatible, {incomplete} incomplete, "
                f"{unquoted} validated but unquoted, "
                f"{speculative} relying on speculative parts"
            ),
            "by_state": counts,
            "incompatible": errors,
            "incomplete": incomplete,
            "unquoted": unquoted,
            "speculative": speculative,
        }
