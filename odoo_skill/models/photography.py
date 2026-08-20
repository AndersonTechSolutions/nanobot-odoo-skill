"""
Product photography operations for the ``product_photography`` module
(Odoo 17 ``photo.session`` / ``photo.session.line`` / ``photo.digitization``).

The module runs the studio floor as a walk: a photographer opens a session,
the module builds a pick list ordered by ``walk_sequence`` through the
warehouse, and each line moves ``basketed -> picked_up -> shot -> returned``.
Lines that go wrong end at ``cannot_find``, ``damaged`` or ``skipped``.
Separately, ``photo.digitization`` is the AI cleanup pipeline for shot
images: ``draft -> queued -> processing -> review -> approved -> published``,
failing to ``failed`` or ``rejected``.

Like FB Marketplace, these models are group-gated (``Photography / User`` or
``/ Manager``) — an API user outside them gets an access fault on every call,
not a partial read. :meth:`BaseOps.access_check` names the missing group.

Two things shape this class:

* **Stock actually moves.** A picked-up line has left its warehouse location
  on a real ``stock.move``, and a session that closes with lines still in
  ``picked_up`` means physical inventory is sitting in the studio, unaccounted
  for. :meth:`PhotographyOps.stranded_lines` is the query that finds it, and
  :meth:`close_session` reports what it is about to strand rather than closing
  silently.

* **``minutes_at_studio`` is** ``searchable: False`` **, so it cannot be
  filtered server-side.** Anything ranking lines by dwell time goes through
  :meth:`BaseOps.search_computed` over a bounded scan.

Digitization records are almost entirely readonly — the pipeline owns its own
state — so this class reads them and does not offer transitions.
"""

import logging
from typing import Any, Optional

from ._base import BaseOps

logger = logging.getLogger("odoo_skill")

_LIST_FIELDS = [
    "id", "name", "user_id", "state", "start_date", "close_date",
    "lines_total_count", "lines_shot_count", "lines_picked_up_count",
]

_DETAIL_FIELDS = _LIST_FIELDS + [
    "duration_minutes", "note", "pickup_picking_id",
    "lines_basketed_count", "lines_returned_count",
    "lines_skipped_count", "lines_damaged_count",
]

_LINE_FIELDS = [
    "id", "session_id", "product_id", "state", "walk_sequence",
    "photo_target", "pickup_serial", "photos_added_count",
    "pickup_time", "shot_time", "return_time",
    "source_location_id", "expected_location_id", "investigation_note",
]

_DIGI_FIELDS = [
    "id", "name", "state", "assigned_user_id", "source_product_name",
    "attempt_count", "queued_at", "processing_at", "review_at",
    "approved_at", "published_at", "completed_at",
    "error_message", "rejection_reason", "product_image_id",
    "session_line_id", "requested_by_id",
]

#: The groups that can reach the photography models at all.
PHOTO_GROUPS = (
    "product_photography.group_photo_user",
    "product_photography.group_photo_manager",
)

#: ``photo.session.state`` values.
SESSION_STATES = ["open", "closed"]

#: ``photo.session.line.state`` values.
LINE_STATES = [
    "basketed", "picked_up", "shot", "returned",
    "cannot_find", "damaged", "skipped",
]

#: Line states meaning the item is physically out of its warehouse location.
OFF_SHELF_STATES = ["picked_up", "shot"]

#: Line states meaning the walk could not complete the item.
PROBLEM_STATES = ["cannot_find", "damaged", "skipped"]

#: ``photo.digitization.state`` values, in pipeline order.
DIGI_STATES = [
    "draft", "queued", "processing", "review",
    "approved", "published", "failed", "rejected", "cancelled",
]

#: Digitization states that are still moving.
DIGI_ACTIVE = ["draft", "queued", "processing", "review", "approved"]


class PhotographyOps(BaseOps):
    """Operations on photo sessions, their walk lines, and the AI pipeline."""

    MODEL = "photo.session"
    MODULE = "product_photography"
    LINE_MODEL = "photo.session.line"
    DIGI_MODEL = "photo.digitization"
    LIST_FIELDS = _LIST_FIELDS
    DETAIL_FIELDS = _DETAIL_FIELDS
    ORDER = "start_date desc"
    REQUIRED_GROUPS = PHOTO_GROUPS

    ALLOWED_ACTIONS = frozenset({
        "action_end",
    })

    # ── Sessions ─────────────────────────────────────────────────────

    def open_sessions(self, limit: int = 20) -> list[dict]:
        """Photo sessions currently running."""
        return self.search([["state", "=", "open"]], limit=limit)

    def sessions_for_photographer(self, user_id: int, limit: int = 20) -> list[dict]:
        """Sessions run by one photographer, newest first."""
        return self.search([["user_id", "=", user_id]], limit=limit)

    def session_progress(self, session_id: int) -> dict:
        """Where a session stands, by line state."""
        record = self.get(session_id)
        total = record.get("lines_total_count") or 0
        shot = record.get("lines_shot_count") or 0
        returned = record.get("lines_returned_count") or 0
        off_shelf = self.client.search_count(
            self.LINE_MODEL,
            [["session_id", "=", session_id],
             ["state", "in", OFF_SHELF_STATES]],
        )
        problems = self.client.search_count(
            self.LINE_MODEL,
            [["session_id", "=", session_id], ["state", "in", PROBLEM_STATES]],
        )
        pct = round(100.0 * returned / total, 1) if total else 0.0
        return {
            "summary": (
                f"Session {record['name']}: {returned}/{total} returned ({pct}%), "
                f"{shot} shot, {off_shelf} still off-shelf, {problems} problem lines"
            ),
            "total": total,
            "shot": shot,
            "returned": returned,
            "off_shelf": off_shelf,
            "problems": problems,
            "percent_complete": pct,
            "session": record,
        }

    # ── Lines ────────────────────────────────────────────────────────

    def get_lines(
        self, session_id: int, state: Optional[str] = None, limit: int = 200
    ) -> list[dict]:
        """A session's pick list, in walk order.

        Args:
            session_id: Session to read.
            state: Optionally restrict to one of :data:`LINE_STATES`.
            limit: Maximum lines to return.
        """
        self._require()
        if state and state not in LINE_STATES:
            raise ValueError(f"state must be one of {LINE_STATES}, got {state!r}")
        domain: list = [["session_id", "=", session_id]]
        if state:
            domain.append(["state", "=", state])
        return self.client.search_read(
            self.LINE_MODEL, domain, fields=_LINE_FIELDS,
            limit=limit, order="walk_sequence, id",
        )

    def stranded_lines(self, limit: int = 100) -> list[dict]:
        """Items off the shelf in a session that has already closed.

        This is real inventory sitting in the studio that the system thinks it
        moved and never got back — the query worth running before a stock
        count, not just a tidiness check.
        """
        self._require()
        return self.client.search_read(
            self.LINE_MODEL,
            [["state", "in", OFF_SHELF_STATES],
             ["session_id.state", "=", "closed"]],
            fields=_LINE_FIELDS, limit=limit, order="session_id, walk_sequence",
        )

    def problem_lines(self, limit: int = 100) -> list[dict]:
        """Lines the walk could not complete — not found, damaged, or skipped."""
        self._require()
        return self.client.search_read(
            self.LINE_MODEL, [["state", "in", PROBLEM_STATES]],
            fields=_LINE_FIELDS, limit=limit, order="id desc",
        )

    def slow_lines(
        self, session_id: Optional[int] = None, min_minutes: float = 60.0,
        limit: int = 50,
    ) -> list[dict]:
        """Lines that have sat at the studio longer than *min_minutes*.

        ``minutes_at_studio`` is computed with ``searchable: False`` — a
        domain on it is silently dropped and would return every line — so this
        filters client-side over a bounded scan.
        """
        self._require()
        domain: list = [["state", "in", OFF_SHELF_STATES]]
        if session_id:
            domain.append(["session_id", "=", session_id])
        want = _LINE_FIELDS + ["minutes_at_studio"]
        window = self._scan_window(limit)
        rows = self.client.search_read(
            self.LINE_MODEL, domain, fields=want, limit=window,
            order="pickup_time asc",
        )
        if len(rows) >= window:
            logger.warning(
                "photo.session.line: slow_lines scanned the full %d-row "
                "window; narrow by session_id.", window,
            )
        hits = [
            r for r in rows if (r.get("minutes_at_studio") or 0) >= min_minutes
        ]
        return hits[:limit]

    # ── Digitization pipeline ────────────────────────────────────────

    def digitizations(
        self, state: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """AI cleanup jobs, optionally restricted to one pipeline state."""
        self._require()
        if state and state not in DIGI_STATES:
            raise ValueError(f"state must be one of {DIGI_STATES}, got {state!r}")
        domain = [["state", "=", state]] if state else []
        return self.client.search_read(
            self.DIGI_MODEL, domain, fields=_DIGI_FIELDS,
            limit=limit, order="id desc",
        )

    def awaiting_review(self, limit: int = 50) -> list[dict]:
        """Processed images waiting on a human to accept or reject them."""
        self._require()
        return self.client.search_read(
            self.DIGI_MODEL, [["state", "=", "review"]],
            fields=_DIGI_FIELDS, limit=limit, order="review_at asc",
        )

    def failed_digitizations(self, limit: int = 50) -> list[dict]:
        """Cleanup jobs that errored, with their messages."""
        self._require()
        return self.client.search_read(
            self.DIGI_MODEL, [["state", "in", ["failed", "rejected"]]],
            fields=_DIGI_FIELDS, limit=limit, order="id desc",
        )

    def stuck_digitizations(self, min_attempts: int = 3, limit: int = 50) -> list[dict]:
        """Jobs that have been retried repeatedly without publishing.

        ``attempt_count`` is computed and not stored, so the retry count is
        evaluated client-side over the still-active set.
        """
        self._require()
        rows = self.client.search_read(
            self.DIGI_MODEL, [["state", "in", DIGI_ACTIVE]],
            fields=_DIGI_FIELDS, limit=self.COMPUTED_SCAN_CAP, order="id desc",
        )
        if len(rows) >= self.COMPUTED_SCAN_CAP:
            logger.warning(
                "photo.digitization: stuck_digitizations scanned the full "
                "%d-row cap; a stuck job older than that window is not "
                "reported.", self.COMPUTED_SCAN_CAP,
            )
        hits = [r for r in rows if (r.get("attempt_count") or 0) >= min_attempts]
        return hits[:limit]

    # ── Writes ───────────────────────────────────────────────────────

    def create_session(
        self, user_id: Optional[int] = None, note: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Open a photo session.

        Args:
            user_id: Photographer; defaults to the API user.
            note: Free text on the session.
            **extra: Any other ``photo.session`` field.
        """
        values: dict[str, Any] = {"user_id": user_id or self.client.uid}
        if note:
            values["note"] = note
        values.update(extra)
        record = self.create(values)
        return {
            "summary": f"Photo session {record['name']} opened",
            "session": record,
        }

    def close_session(self, session_id: int, force: bool = False) -> dict:
        """Close a session, reporting anything it would strand.

        Refuses by default when lines are still off the shelf — closing over
        them leaves real inventory in the studio with nothing tracking it.
        Pass ``force=True`` to close anyway (sometimes the right call at the
        end of a shift); the stranded lines are reported either way and stay
        findable via :meth:`stranded_lines`.
        """
        # Query the off-shelf lines directly instead of fetching a page of
        # lines and filtering. Fetching the first N and filtering means a
        # session with more than N lines can hide a stranded one past the
        # window — the guard then sees nothing and closes over real inventory,
        # which is the exact failure it exists to prevent.
        stranded_domain = [
            ["session_id", "=", session_id],
            ["state", "in", OFF_SHELF_STATES],
        ]
        stranded_count = self.client.search_count(self.LINE_MODEL, stranded_domain)
        stranded = self.client.search_read(
            self.LINE_MODEL, stranded_domain, fields=_LINE_FIELDS,
            limit=200, order="walk_sequence, id",
        ) if stranded_count else []
        if stranded_count and not force:
            return {
                "summary": (
                    f"Session not closed — {stranded_count} line(s) are still "
                    "off the shelf. Return them first, or call again with "
                    "force=True to close anyway."
                ),
                "closed": False,
                "stranded_count": stranded_count,
                "stranded_lines": stranded,
            }
        result = self.run_action(session_id, "action_end")
        return {
            "summary": (
                f"Session {result['record'].get('name')} closed"
                + (f" — {stranded_count} line(s) left off-shelf"
                   if stranded_count else "")
            ),
            "closed": True,
            "stranded_count": stranded_count,
            "stranded_lines": stranded,
            "session": result["record"],
        }

    def note_line(self, line_id: int, note: str) -> dict:
        """Record an investigation note on a problem line."""
        self._require()
        self.client.write(self.LINE_MODEL, line_id, {"investigation_note": note})
        rows = self.client.read(self.LINE_MODEL, [line_id], fields=_LINE_FIELDS)
        return {
            "summary": f"Note saved on photo line {line_id}",
            "line": rows[0] if rows else {},
        }

    # ── Summary ──────────────────────────────────────────────────────

    def studio_summary(self) -> dict:
        """Sessions, stranded stock, and the digitization backlog."""
        self._require()
        open_sessions = self.count([["state", "=", "open"]])
        off_shelf = self.client.search_count(
            self.LINE_MODEL, [["state", "in", OFF_SHELF_STATES]]
        )
        stranded = self.client.search_count(
            self.LINE_MODEL,
            [["state", "in", OFF_SHELF_STATES],
             ["session_id.state", "=", "closed"]],
        )
        problems = self.client.search_count(
            self.LINE_MODEL, [["state", "in", PROBLEM_STATES]]
        )
        review = self.client.search_count(self.DIGI_MODEL, [["state", "=", "review"]])
        failed = self.client.search_count(
            self.DIGI_MODEL, [["state", "in", ["failed", "rejected"]]]
        )
        return {
            "summary": (
                f"Studio: {open_sessions} open session(s), {off_shelf} items "
                f"off-shelf ({stranded} stranded in closed sessions), "
                f"{problems} problem lines, {review} images awaiting review, "
                f"{failed} digitizations failed"
            ),
            "open_sessions": open_sessions,
            "off_shelf": off_shelf,
            "stranded": stranded,
            "problem_lines": problems,
            "awaiting_review": review,
            "failed_digitizations": failed,
        }
