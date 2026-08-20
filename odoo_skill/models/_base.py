"""
Shared base for the AndersonTech custom-module operation classes.

The original twelve ops classes (partner, sale_order, ...) each stand alone.
The custom-module classes added later share enough behaviour — module
availability guards, gated action dispatch, consistent summaries — that
duplicating it eleven times would be worse than a small base.

Two rules this base enforces, both of which matter for unattended agents:

1. **Module guards.** Not every AndersonTech module is installed on every
   database (staging vs prod vs a fresh dev DB). An ops class declares
   ``REQUIRES_MODEL``; calling into it on a database where that model is
   absent raises a clear :class:`OdooModuleNotInstalledError` naming the
   module, instead of a cryptic XML-RPC fault.

2. **Gated action dispatch.** ``run_action`` will only invoke a method
   listed in the class's ``ALLOWED_ACTIONS``. Odoo's ``execute_kw`` will
   happily call *any* public method on a model — including ``unlink`` and
   anything a future module adds. An agent that can be talked into calling
   an arbitrary method name is a liability, so the allowlist is explicit
   and per-class.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from ..client import OdooClient
from ..errors import OdooError, OdooRecordNotFoundError

logger = logging.getLogger("odoo_skill")


class OdooModuleNotInstalledError(OdooError):
    """Raised when an ops class is used against a database lacking its module."""


class OdooActionNotAllowedError(OdooError):
    """Raised when a caller asks for a method outside the class allowlist."""


class BaseOps:
    """Common behaviour for custom-module operation classes.

    Subclasses set :attr:`MODEL`, :attr:`MODULE`, :attr:`LIST_FIELDS`,
    :attr:`DETAIL_FIELDS`, and :attr:`ALLOWED_ACTIONS`.
    """

    #: Primary Odoo model this class operates on.
    MODEL: str = ""
    #: Technical name of the Odoo module providing :attr:`MODEL` (for errors).
    MODULE: str = ""
    #: Fields returned by :meth:`search` / list views.
    LIST_FIELDS: list[str] = ["id", "display_name"]
    #: Fields returned by :meth:`get` / detail views. Falls back to LIST_FIELDS.
    DETAIL_FIELDS: list[str] = []
    #: Method names :meth:`run_action` is permitted to invoke.
    ALLOWED_ACTIONS: frozenset[str] = frozenset()
    #: Default ordering for searches.
    ORDER: str = ""
    #: Odoo group xmlids the API user needs to reach :attr:`MODEL` at all.
    #: Set on classes whose module ships restrictive ``ir.model.access`` rows —
    #: see :meth:`access_check`.
    REQUIRED_GROUPS: tuple[str, ...] = ()

    def __init__(self, client: OdooClient) -> None:
        self.client = client
        self._available: Optional[bool] = None

    # ── Availability ─────────────────────────────────────────────────

    def available(self) -> bool:
        """Return ``True`` if :attr:`MODEL` exists on this database.

        Cached per instance — the answer cannot change within a session.
        """
        if self._available is None:
            try:
                self.client.fields_get(self.MODEL, attributes=["type"])
                self._available = True
            except OdooError:
                self._available = False
                logger.info(
                    "Model %s unavailable (module %s not installed)",
                    self.MODEL, self.MODULE or "?",
                )
        return self._available

    def _require(self) -> None:
        """Raise if the backing module is not installed."""
        if not self.available():
            raise OdooModuleNotInstalledError(
                f"Model '{self.MODEL}' is not available on this database. "
                f"Install the '{self.MODULE or 'providing'}' module first."
            )

    def access_check(self) -> dict:
        """Report whether the API user can actually read :attr:`MODEL`.

        There are two very different reasons an ops class returns nothing
        useful, and the raw faults do not distinguish them well: the module is
        not installed, or it is installed but the API user is outside the
        groups its ``ir.model.access`` rows name. The second is the one that
        bites in practice — it is invisible until the first call, it is
        all-or-nothing rather than a partial read, and its fault text is a
        wall of group names. Both collapse to one diagnosable answer here,
        naming the group to grant when that is the problem.

        Subclasses only need to set :attr:`REQUIRED_GROUPS` for the message to
        name the right groups; the check itself works regardless.
        """
        if not self.available():
            return {
                "ok": False,
                "reason": "module_not_installed",
                "summary": (
                    f"The '{self.MODULE or 'providing'}' module is not "
                    f"installed on this database ({self.MODEL} is absent)."
                ),
                "model": self.MODEL,
            }
        try:
            self.client.search_count(self.MODEL, [])
        except OdooError as exc:
            groups = list(self.REQUIRED_GROUPS)
            hint = (
                f" Add it to {groups[0]}"
                + (" (or one of: " + ", ".join(groups[1:]) + ")"
                   if len(groups) > 1 else "")
                + " in Odoo → Settings → Users & Companies → Users."
                if groups else
                " Check the ir.model.access rows for this model."
            )
            return {
                "ok": False,
                "reason": "no_access",
                "summary": (
                    f"The API user cannot read {self.MODEL}.{hint}"
                ),
                "model": self.MODEL,
                "required_groups": groups,
                "error": str(exc)[:300],
            }
        return {
            "ok": True,
            "summary": f"{self.MODEL} is readable by the API user.",
            "model": self.MODEL,
        }

    # ── Read ─────────────────────────────────────────────────────────

    def _fields(self, detail: bool = False) -> list[str]:
        if detail:
            return self.DETAIL_FIELDS or self.LIST_FIELDS
        return self.LIST_FIELDS

    def search(
        self,
        domain: Optional[list] = None,
        limit: int = 50,
        offset: int = 0,
        order: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> list[dict]:
        """Search records, returning list-view fields."""
        self._require()
        return self.client.search_read(
            self.MODEL,
            domain or [],
            fields=fields or self._fields(),
            limit=limit,
            offset=offset,
            order=order if order is not None else self.ORDER,
        )

    def get(self, rec_id: int, fields: Optional[list[str]] = None) -> dict:
        """Read one record with detail fields.

        Raises:
            OdooRecordNotFoundError: If *rec_id* does not exist.
        """
        self._require()
        rows = self.client.read(
            self.MODEL, [rec_id], fields=fields or self._fields(detail=True)
        )
        if not rows:
            raise OdooRecordNotFoundError(
                f"No {self.MODEL} record with id {rec_id}"
            )
        return rows[0]

    def count(self, domain: Optional[list] = None) -> int:
        """Count records matching *domain*."""
        self._require()
        return self.client.search_count(self.MODEL, domain or [])

    def find(self, query: str, field: str = "name", limit: int = 10) -> list[dict]:
        """Case-insensitive lookup on *field*."""
        return self.search([[field, "ilike", query]], limit=limit)

    # ── Non-stored computed fields ───────────────────────────────────
    #
    # Several of the custom modules expose useful flags as *non-stored*
    # computed fields — repair.order.is_overdue, rma.order.can_execute_resolutions,
    # tasks.itad_can_dispatch, and others.
    #
    # The failure mode is nasty: rather than raising, Odoo drops the clause
    # and returns the *unfiltered* set. A caller asking for "overdue repairs"
    # gets every open repair back with no indication anything went wrong — it
    # logs an error with a traceback server-side, but nothing reaches the RPC
    # caller. So any filter on such a field runs client-side, over a bounded
    # scan.
    #
    # IMPORTANT — the discriminator is ``searchable``, not ``store``. A
    # non-stored field is still searchable when it is ``related=`` to a stored
    # one, or when its definition supplies a ``search=`` method; in that case
    # Odoo rewrites the domain and resolves it server-side, correctly. Only
    # ``searchable: False`` fields get dropped. Checking ``store`` alone
    # over-flags and pushes filters client-side that did not need to be, which
    # is strictly worse — a client-side scan is capped at COMPUTED_SCAN_CAP
    # rows and silently under-reports past it, where a server-side domain is
    # exact.
    #
    # Confirm before assuming, e.g.:
    #     fields_get([field], attributes=["store", "searchable"])
    # and sanity-check that a filtered count differs from the unfiltered one.
    #
    # Verified searchable: False (need this path) — repair.order.is_overdue,
    # repair.order.is_awaiting_parts, rma.order.can_execute_resolutions,
    # tasks.itad_can_dispatch / itad_can_price / itad_can_receive,
    # tasks.sla_days_remaining.
    # Verified searchable: True (filter server-side) — project.task.is_fsm,
    # rma.order.advance_return_overdue.

    #: How many rows to pull per requested row when filtering client-side.
    COMPUTED_SCAN_FACTOR: int = 10
    #: Hard ceiling on a client-side scan, to bound one RPC round-trip.
    COMPUTED_SCAN_CAP: int = 2000

    def _scan_window(self, limit: int) -> int:
        """How many rows to fetch when a predicate must run client-side."""
        return min(max(limit * self.COMPUTED_SCAN_FACTOR, 200), self.COMPUTED_SCAN_CAP)

    def search_computed(
        self,
        stored_domain: Optional[list],
        predicate: "Callable[[dict], bool]",
        limit: int = 50,
        order: Optional[str] = None,
        fields: Optional[list[str]] = None,
        extra_fields: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """Search on stored fields, then apply *predicate* in Python.

        Args:
            stored_domain: Domain over stored fields only — narrows the scan.
            predicate: Called per row; return ``True`` to keep it.
            limit: Maximum rows to return after filtering.
            order: Sort clause applied server-side, before filtering.
            fields: Fields to return (defaults to the class list fields).
            extra_fields: Fields the predicate needs that are not in *fields*.

        Returns:
            At most *limit* matching rows. When the scan window fills up, a
            warning is logged — the result is then a prefix, not the whole
            truth, and the caller should narrow *stored_domain*.
        """
        self._require()
        want = list(fields or self._fields())
        for f in extra_fields or []:
            if f not in want:
                want.append(f)

        window = self._scan_window(limit)
        rows = self.client.search_read(
            self.MODEL, stored_domain or [], fields=want,
            limit=window, order=order if order is not None else self.ORDER,
        )
        if len(rows) >= window:
            logger.warning(
                "%s: client-side filter scanned the full %d-row window; "
                "results may be incomplete. Narrow the stored domain.",
                self.MODEL, window,
            )
        return [r for r in rows if predicate(r)][:limit]

    def count_computed(
        self,
        stored_domain: Optional[list],
        predicate: "Callable[[dict], bool]",
        extra_fields: Optional[Iterable[str]] = None,
    ) -> int:
        """Count rows matching a client-side *predicate*.

        Bounded by :attr:`COMPUTED_SCAN_CAP`. The count is exact only when
        the stored domain selects fewer rows than the cap; otherwise it is a
        floor and a warning is logged.
        """
        self._require()
        want = ["id"] + [f for f in (extra_fields or []) if f != "id"]
        rows = self.client.search_read(
            self.MODEL, stored_domain or [], fields=want,
            limit=self.COMPUTED_SCAN_CAP,
        )
        if len(rows) >= self.COMPUTED_SCAN_CAP:
            logger.warning(
                "%s: count_computed hit the %d-row cap; the returned count is "
                "a lower bound.", self.MODEL, self.COMPUTED_SCAN_CAP,
            )
        return sum(1 for r in rows if predicate(r))

    # ── Write ────────────────────────────────────────────────────────

    def create(self, values: dict) -> dict:
        """Create a record and return it in detail form."""
        self._require()
        rec_id = self.client.create(self.MODEL, values)
        logger.info("Created %s id=%s", self.MODEL, rec_id)
        return self.get(rec_id)

    def update(self, rec_id: int, values: dict) -> dict:
        """Write *values* to a record and return the updated detail form."""
        self._require()
        self.client.write(self.MODEL, rec_id, values)
        logger.info("Updated %s id=%s fields=%s", self.MODEL, rec_id, list(values))
        return self.get(rec_id)

    # ── Gated action dispatch ────────────────────────────────────────

    def run_action(self, rec_id: int, method: str, **kwargs: Any) -> dict:
        """Invoke an allowlisted button method on a record.

        Odoo button methods return either ``True``/``False`` or an action
        dict (to open a view/wizard). Both are wrapped in a consistent
        envelope alongside the record's post-action state, so a caller can
        see what actually changed.

        Args:
            rec_id: Record to act on.
            method: Method name; must be in :attr:`ALLOWED_ACTIONS`.
            **kwargs: Forwarded to the Odoo method.

        Raises:
            OdooActionNotAllowedError: If *method* is not allowlisted.
        """
        self._require()
        if method not in self.ALLOWED_ACTIONS:
            raise OdooActionNotAllowedError(
                f"Method '{method}' is not permitted on {self.MODEL}. "
                f"Allowed: {', '.join(sorted(self.ALLOWED_ACTIONS)) or '(none)'}"
            )
        raw = self.client.execute(self.MODEL, method, [rec_id], **kwargs)
        record = self.get(rec_id)
        return {
            "model": self.MODEL,
            "id": rec_id,
            "method": method,
            "returned": raw if not isinstance(raw, dict) else _describe_action(raw),
            "record": record,
        }

    def actions(self) -> list[str]:
        """List the methods :meth:`run_action` will accept."""
        return sorted(self.ALLOWED_ACTIONS)

    def _call_model(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call an ``@api.model`` (model-level) method on :attr:`MODEL`.

        The counterpart to :meth:`run_action`, and the distinction is not
        cosmetic. Odoo dispatches record methods through ``_call_kw_multi``,
        which strips the leading element of the argument vector and browses it
        as ids; model-level methods go through ``_call_kw_model``, which
        forwards every positional straight to the callee.

        So a model-level method must be called with **no ids list** — passing
        one silently becomes a real argument and produces "takes N positional
        arguments but N+1 were given". Routing these through a named helper
        keeps that asymmetry visible at the call site instead of leaving it to
        be rediscovered against a live server.

        Args:
            method: Model-level method name.
            *args: The method's own declared positionals — *not* an ids list.
            **kwargs: Forwarded as keyword arguments.
        """
        self._require()
        return self.client.execute(self.MODEL, method, *args, **kwargs)

    # ── Helpers for subclasses ───────────────────────────────────────

    def _resolve_one(
        self,
        query: Any,
        model: str,
        field: str = "name",
        extra_domain: Optional[list] = None,
    ) -> Optional[dict]:
        """Resolve *query* (an id or a name) to a single ``{id, name}`` dict.

        Returns ``None`` when nothing matches. Ambiguity resolves to the
        first match by Odoo's default order — callers that care should use
        :meth:`_resolve_candidates` instead.
        """
        if isinstance(query, int):
            rows = self.client.read(model, [query], fields=["display_name"])
            return {"id": query, "name": rows[0]["display_name"]} if rows else None
        domain = list(extra_domain or []) + [[field, "ilike", str(query)]]
        rows = self.client.search_read(
            model, domain, fields=["display_name"], limit=1
        )
        return {"id": rows[0]["id"], "name": rows[0]["display_name"]} if rows else None

    def _resolve_candidates(
        self,
        query: str,
        model: str,
        field: str = "name",
        limit: int = 5,
        extra_domain: Optional[list] = None,
    ) -> list[dict]:
        """Return near-matches for *query*, for 'did you mean' responses."""
        domain = list(extra_domain or []) + [[field, "ilike", str(query)]]
        rows = self.client.search_read(
            model, domain, fields=["display_name"], limit=limit
        )
        return [{"id": r["id"], "name": r["display_name"]} for r in rows]


def _describe_action(action: dict) -> dict:
    """Compress an Odoo action dict to the bits a chat agent can use."""
    return {
        "type": action.get("type"),
        "res_model": action.get("res_model"),
        "res_id": action.get("res_id"),
        "name": action.get("name"),
        "view_mode": action.get("view_mode"),
        "target": action.get("target"),
        "tag": action.get("tag"),
    }


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def utc_stamp(offset: Optional[timedelta] = None) -> str:
    """An Odoo-format UTC timestamp, optionally shifted by *offset*.

    Odoo stores and compares datetimes as naive UTC strings, so the tzinfo is
    dropped after the arithmetic — computing in aware UTC and formatting naive
    is the same wall time without the ``utcnow()`` deprecation.
    """
    moment = utc_now() + (offset or timedelta())
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def summarize(rows: Iterable[dict], label: str, empty: str = "none") -> str:
    """Build a one-line human summary of a result set."""
    rows = list(rows)
    if not rows:
        return f"No {label} found ({empty})."
    names = [str(r.get("display_name") or r.get("name") or r.get("id")) for r in rows[:5]]
    more = f" (+{len(rows) - 5} more)" if len(rows) > 5 else ""
    return f"{len(rows)} {label}: " + ", ".join(names) + more
