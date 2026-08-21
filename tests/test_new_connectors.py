"""Tests for the connectors added alongside FB Marketplace.

These assert the decisions that a live smoke test cannot catch, because a
database with the right data in it happens to make the wrong call look fine:

* Domains that must stay **server-side** vs the ones that must run
  **client-side**. A domain on a ``searchable: False`` field does not raise —
  Odoo drops the clause and returns the unfiltered set — so "stale listings"
  silently becomes "all listings". The only way to pin that down is to assert
  on the domain actually put on the wire.
* Fields that must be written **together** (bid approval, receipt
  confirmation), because writing half of them produces a record that is
  quietly wrong rather than one that errors.
* Guards that must **refuse**: sending an empty reply to a buyer, quoting an
  incompatible build, approving a bid with no ceiling.
* Secrets that must **not** appear in list output.
"""

import os
import sys

import pytest

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from odoo_skill.errors import OdooError  # noqa: E402
from odoo_skill.models.ebay_messages import EbayMessageOps  # noqa: E402
from odoo_skill.models.fb_marketplace import FbMarketplaceOps  # noqa: E402
from odoo_skill.models.inbound import InboundOps  # noqa: E402
from odoo_skill.models.order_status import OrderStatusOps  # noqa: E402
from odoo_skill.models.pc_build import PcBuildOps  # noqa: E402
from odoo_skill.models.photography import PhotographyOps  # noqa: E402


def _calls(mock_client):
    """Every execute_kw call as (model, method, args_vector, odoo_kwargs).

    Note the last element: ``OdooClient.execute`` passes Odoo's keyword dict as
    the *seventh positional* argument to ``execute_kw`` (db, uid, key, model,
    method, args, kwargs), not as Python kwargs. Reading ``call[1]`` therefore
    always yields ``{}`` and silently hides ``fields``/``limit``/``order``.
    """
    out = []
    for call in mock_client._models.execute_kw.call_args_list:
        args = call[0]
        odoo_kwargs = args[6] if len(args) > 6 else {}
        out.append((args[3], args[4], args[5], odoo_kwargs))
    return out


def _ready(cls, mock_client):
    ops = cls(mock_client)
    ops._available = True
    # Empty (not None) means "schema unknown" — _fields() then skips
    # filtering, so the mock's execute_kw sequence stays untouched.
    ops._model_field_cache = set()
    return ops


@pytest.fixture()
def fb(mock_client):
    return _ready(FbMarketplaceOps, mock_client)


@pytest.fixture()
def inbound(mock_client):
    return _ready(InboundOps, mock_client)


@pytest.fixture()
def ebay(mock_client):
    return _ready(EbayMessageOps, mock_client)


@pytest.fixture()
def builds(mock_client):
    return _ready(PcBuildOps, mock_client)


@pytest.fixture()
def status(mock_client):
    return _ready(OrderStatusOps, mock_client)


@pytest.fixture()
def photo(mock_client):
    return _ready(PhotographyOps, mock_client)


# ── FB Marketplace ───────────────────────────────────────────────────


class TestFbMarketplace:

    def test_stale_listings_does_not_put_days_listed_in_the_domain(
        self, fb, mock_client
    ):
        """``days_listed`` is searchable: False — a domain on it is dropped.

        If this regresses, ``stale_listings`` returns every live listing and
        looks entirely plausible while doing it.
        """
        mock_client._models.execute_kw.return_value = [
            {"id": 1, "days_listed": 90},
            {"id": 2, "days_listed": 3},
        ]
        rows = fb.stale_listings(older_than_days=30)

        model, method, args, _ = _calls(mock_client)[0]
        domain = args[0]
        assert not any(
            isinstance(clause, (list, tuple)) and clause[0] == "days_listed"
            for clause in domain
        ), f"days_listed must not reach the server: {domain}"
        assert [r["id"] for r in rows] == [1], "filter must run client-side"

    def test_renewal_date_filter_stays_server_side(self, fb, mock_client):
        """``renewal_date`` IS stored and searchable — keep it on the wire."""
        mock_client._models.execute_kw.return_value = []
        fb.renewal_due(within_days=3)
        _, _, args, _ = _calls(mock_client)[0]
        flat = str(args[0])
        assert "renewal_date" in flat
        assert "<=" in flat

    def test_set_price_writes_the_product_not_the_listing(self, fb, mock_client):
        """``price`` is related+readonly; writing it on the listing raises."""
        mock_client._models.execute_kw.side_effect = [
            [{"id": 7, "name": "Dell 7050", "product_tmpl_id": [42, "Dell 7050"]}],
            True,
            [{"id": 7, "name": "Dell 7050", "product_tmpl_id": [42, "Dell 7050"]}],
        ]
        fb.set_price(7, 249.0)
        writes = [c for c in _calls(mock_client) if c[1] == "write"]
        assert writes, "expected a write"
        model, _, args, _ = writes[0]
        assert model == "product.template"
        assert args[0] == [42]
        assert args[1] == {"list_price": 249.0}

    def test_create_listing_rejects_an_unknown_condition(self, fb):
        with pytest.raises(ValueError, match="condition must be one of"):
            fb.create_listing(product_tmpl_id=1, condition="mint")

    def test_mark_listed_refuses_without_a_marketplace_url(self, fb, mock_client):
        """The module raises on a listed record with no URL — catch it first."""
        mock_client._models.execute_kw.return_value = [
            {"id": 7, "name": "Dell", "listing_url": False},
        ]
        with pytest.raises(ValueError, match="listing_url is required"):
            fb.mark_listed(7)
        assert "action_mark_listed" not in [c[1] for c in _calls(mock_client)]

    def test_mark_listed_writes_the_url_before_acting(self, fb, mock_client):
        mock_client._models.execute_kw.side_effect = [
            True,                                                  # write url
            [{"id": 7, "name": "Dell", "listing_url": "https://fb/x"}],
            True,                                                  # action
            [{"id": 7, "name": "Dell", "state": "listed"}],
        ]
        fb.mark_listed(7, listing_url="https://fb/x")
        calls = _calls(mock_client)
        write_idx = next(i for i, c in enumerate(calls) if c[1] == "write")
        act_idx = next(i for i, c in enumerate(calls) if c[1] == "action_mark_listed")
        assert write_idx < act_idx, "URL must be written before the action fires"
        assert calls[write_idx][2][1] == {"listing_url": "https://fb/x"}

    def test_required_groups_are_declared(self, fb):
        """access_check can only name the group if the class declares it."""
        assert fb.REQUIRED_GROUPS
        assert all("group_fb_marketplace" in g for g in fb.REQUIRED_GROUPS)

    def test_get_image_data_requests_the_binary_field(self, fb, mock_client):
        """get_image_data is the ONE read that pulls the base64 ``image``.

        get_images withholds it on purpose; if this read stops asking for
        ``image`` the FB lister silently has no bytes to upload.
        """
        mock_client._models.execute_kw.return_value = [
            {"id": 5, "name": "front", "sequence": 10, "image": "aGk="},
        ]
        rows = fb.get_image_data(7)

        model, method, args, odoo_kwargs = _calls(mock_client)[0]
        assert model == fb.IMAGE_MODEL
        assert method == "search_read"
        assert args[0] == [["listing_id", "=", 7]]
        assert "image" in odoo_kwargs["fields"], (
            f"image must be requested, got {odoo_kwargs['fields']}"
        )
        assert odoo_kwargs["limit"] == 50, "binary read stays explicitly bounded"
        assert rows[0]["image"] == "aGk="

    def test_get_images_still_withholds_the_binary(self, fb, mock_client):
        """The plain lister must NOT drag base64 into a transcript."""
        mock_client._models.execute_kw.return_value = []
        fb.get_images(7)
        _, _, _, odoo_kwargs = _calls(mock_client)[0]
        assert "image" not in odoo_kwargs["fields"]


# ── Inbound shipments ────────────────────────────────────────────────


class TestInbound:

    def test_confirm_receipt_writes_status_and_confirmer_together(
        self, inbound, mock_client
    ):
        """A confirmed shipment with no confirmer is an audit hole."""
        mock_client._models.execute_kw.side_effect = [
            [],      # get_lines
            True,    # write
            [{"id": 3, "inbound_ref": "IN-3", "status": "confirmed"}],  # re-read
        ]
        inbound.confirm_receipt(3)
        writes = [c for c in _calls(mock_client) if c[1] == "write"]
        values = writes[0][2][1]
        assert values["status"] == "confirmed"
        assert values["confirmed_at"], "confirmed_at must be stamped"
        assert values["confirmed_by"] == mock_client.uid

    def test_confirm_receipt_reports_short_lines_without_blocking(
        self, inbound, mock_client
    ):
        """A short shipment is a real outcome — report it, do not refuse it."""
        mock_client._models.execute_kw.side_effect = [
            [{"id": 1, "qty_expected": 5, "qty_received": 2, "inspected": True}],
            True,
            [{"id": 3, "inbound_ref": "IN-3", "status": "confirmed"}],
        ]
        result = inbound.confirm_receipt(3)
        assert len(result["short_lines"]) == 1
        assert "short" in result["summary"]

    def test_dashboard_sends_no_ids_list(self, inbound, mock_client):
        """``get_dashboard_data`` is @api.model — a leading [] becomes an arg."""
        mock_client._models.execute_kw.return_value = {}
        inbound.dashboard()
        model, method, args, _ = _calls(mock_client)[0]
        assert model == "inbound.shipment"
        assert method == "get_dashboard_data"
        assert args == [], f"model methods take no ids list, got {args!r}"

    def test_create_shipment_rejects_an_unknown_carrier(self, inbound):
        with pytest.raises(ValueError, match="carrier must be one of"):
            inbound.create_shipment("1Z999", carrier="royalmail")


# ── eBay messages ────────────────────────────────────────────────────


class TestEbayMessages:

    def test_send_reply_refuses_an_empty_body(self, ebay):
        with pytest.raises(ValueError, match="empty reply"):
            ebay.send_reply(1, "   ")

    def test_draft_reply_never_sends(self, ebay, mock_client):
        """Generation and sending are separate on purpose."""
        mock_client._models.execute_kw.side_effect = [
            True,
            [{"id": 1, "sender": "buyer", "reply_draft": "hello"}],
        ]
        ebay.draft_reply(1)
        methods = [c[1] for c in _calls(mock_client)]
        assert "action_generate_ai_reply" in methods
        assert "action_send_inline_reply" not in methods

    def test_messages_for_order_does_not_filter_on_order_id(
        self, ebay, mock_client
    ):
        """``ebay.message.order_id`` is searchable: False.

        Filtering on it returns the entire inbox instead of raising, so the
        lookup must resolve via the stored item_id.
        """
        mock_client._models.execute_kw.side_effect = [
            [{"id": 9, "order_id": "12-345", "item_id": "ITEM1"}],  # find_order
            [],                                                     # search
        ]
        ebay.messages_for_order("12-345")
        message_calls = [
            c for c in _calls(mock_client) if c[0] == "ebay.message"
        ]
        assert message_calls, "expected a message search"
        domain = str(message_calls[0][2][0])
        assert "item_id" in domain
        assert "order_id" not in domain

    def test_messages_for_order_resolves_the_order_exactly(self, ebay, mock_client):
        """Authorisation must not use a fuzzy match.

        ``find_order`` is ``ilike``, so resolving "12-345" also matches
        "XX12-345YY" — a different customer's order. Every matched order was
        then treated as authorised, and narrowing afterwards cannot undo a
        too-wide authorisation.
        """
        mock_client._models.execute_kw.side_effect = [
            [{"id": 9, "order_id": "12-345", "item_id": "ITEM1"}],
            [{"id": 1, "subject": "mine",   "order_id": [9, "12-345"]},
             {"id": 2, "subject": "theirs", "order_id": [77, "XX12-345YY"]}],
        ]
        rows = ebay.messages_for_order("12-345")
        domain = _calls(mock_client)[0][2][0]
        assert domain == [["order_id", "=", "12-345"]], (
            f"order resolution must be exact, got {domain}"
        )
        assert [r["id"] for r in rows] == [1]

    def test_messages_for_order_excludes_other_buyers_of_the_same_listing(
        self, ebay, mock_client
    ):
        """item_id identifies the LISTING, not the order.

        A fixed-price listing sells many times, so narrowing on item_id alone
        mixes other customers' correspondence into an answer about one order.
        The requested order must be matched exactly, client-side.
        """
        mock_client._models.execute_kw.side_effect = [
            [{"id": 9, "order_id": "12-345", "item_id": "ITEM1"}],  # find_order
            [                                                       # same listing
                {"id": 1, "subject": "mine",   "order_id": [9, "12-345"]},
                {"id": 2, "subject": "theirs", "order_id": [77, "99-999"]},
                {"id": 3, "subject": "nobody", "order_id": False},
            ],
        ]
        rows = ebay.messages_for_order("12-345")
        assert [r["id"] for r in rows] == [1], (
            "only messages belonging to the resolved order may be returned"
        )

    def test_messages_for_order_returns_empty_when_order_unknown(
        self, ebay, mock_client
    ):
        """No matching order must mean no messages — not every message."""
        mock_client._models.execute_kw.return_value = []
        assert ebay.messages_for_order("nope") == []


# ── PC builds ────────────────────────────────────────────────────────


class TestPcBuild:

    def _detail(self, status, messages="PSU too small"):
        return [
            [{"id": 1, "name": "B1", "compatibility_status": status,
              "compatibility_messages": messages, "total_price": 500,
              "est_power_draw": 450}],
            [{"id": 10, "component_type": "cpu"}],
        ]

    def test_quotation_refused_on_incompatible_build(self, builds, mock_client):
        mock_client._models.execute_kw.side_effect = self._detail("error")
        result = builds.create_quotation(1)
        assert result["ok"] is False
        assert "PSU too small" in result["summary"]
        assert "action_create_quotation" not in [c[1] for c in _calls(mock_client)]

    def test_quotation_proceeds_with_override(self, builds, mock_client):
        mock_client._models.execute_kw.side_effect = self._detail("error") + [
            True,
            [{"id": 1, "name": "B1", "compatibility_status": "error"}],
        ]
        builds.create_quotation(1, override=True)
        assert "action_create_quotation" in [c[1] for c in _calls(mock_client)]

    def test_quotation_proceeds_when_compatible(self, builds, mock_client):
        mock_client._models.execute_kw.side_effect = self._detail("ok", False) + [
            True,
            [{"id": 1, "name": "B1", "compatibility_status": "ok"}],
        ]
        builds.create_quotation(1)
        assert "action_create_quotation" in [c[1] for c in _calls(mock_client)]

    def test_replace_line_requires_the_removed_part(self, builds):
        with pytest.raises(ValueError, match="removed_product_id"):
            builds.add_component(
                1, product_id=5, component_type="ram", upgrade_action="replace"
            )

    def test_base_upgrade_requires_a_base_product(self, builds):
        with pytest.raises(ValueError, match="base_product_id"):
            builds.create_build(build_mode="base_upgrade")


# ── Order status ─────────────────────────────────────────────────────


class TestOrderStatus:

    def test_status_token_is_not_in_the_field_lists(self, status):
        """Listing orders must not spray live capability tokens."""
        assert "status_token" not in status.LIST_FIELDS
        assert "status_token" not in status.DETAIL_FIELDS

    def test_status_link_refuses_a_non_https_base(self, status, mock_client):
        """A typo in the base URL must not leak the token to another host."""
        mock_client._models.execute_kw.side_effect = [
            [{"id": 1, "name": "S001", "status_token": "tok123"}],
            [{"id": 1, "value": "http://insecure.example.com/status"}],
        ]
        result = status.status_link(1)
        assert result["url"] is None
        assert "not https" in result["summary"]

    def test_status_link_builds_ref_and_token(self, status, mock_client):
        mock_client._models.execute_kw.side_effect = [
            [{"id": 1, "name": "S001", "status_token": "tok123"}],
            [{"id": 1, "value": "https://andersontechsolutions.com/order-status"}],
        ]
        result = status.status_link(1)
        assert "ref=S001" in result["url"]
        assert "t=tok123" in result["url"]

    def test_find_by_token_is_exact_not_ilike(self, status, mock_client):
        """A prefix search over tokens would be a guessing oracle."""
        mock_client._models.execute_kw.return_value = []
        status.find_by_token("tok123")
        domain = _calls(mock_client)[0][2][0]
        assert domain == [["status_token", "=", "tok123"]]

    def test_availability_keys_off_the_added_field(self, status, mock_client):
        """``sale.order`` exists everywhere — status_token is the tell."""
        status._available = None
        mock_client._models.execute_kw.return_value = {"name": {"type": "char"}}
        assert status.available() is False


# ── Photography ──────────────────────────────────────────────────────


#: An Odoo fault for a method the installed module is too old to have. The
#: skill uses exactly this shape to decide whether to fall back to the advisory
#: client-side close, so the tests reproduce it rather than a hand-waved string.
def _missing_method_fault(method="action_end_guarded"):
    import xmlrpc.client
    return xmlrpc.client.Fault(
        1,
        "Traceback (most recent call last):\n"
        f"AttributeError: 'photo.session' object has no attribute '{method}'",
    )


class TestPhotography:
    """close_session's primary path is the module's atomic action_end_guarded;
    the advisory two-RPC guard is a fallback for databases without it."""

    # ── Primary path: the server's atomic guard ──────────────────────

    def test_close_session_uses_the_atomic_server_guard(self, photo, mock_client):
        """The single authoritative call is action_end_guarded, not a client
        count-then-close dance."""
        mock_client._models.execute_kw.side_effect = [
            {"closed": False, "stranded_count": 1,
             "stranded_lines": [{"id": 1, "state": "picked_up"}],
             "summary": "Session not closed — 1 line(s) are still off the shelf."},
        ]
        result = photo.close_session(1)
        assert result["closed"] is False
        assert result["stranded_count"] == 1
        methods = [c[1] for c in _calls(mock_client)]
        assert methods == ["action_end_guarded"], (
            "close must delegate to the atomic guard in one RPC, not run its "
            "own count/recheck/close sequence"
        )
        _, _, args, kwargs = _calls(mock_client)[0]
        assert args[0] == [1]
        assert kwargs == {"force": False}

    def test_close_session_forwards_force_to_the_guard(self, photo, mock_client):
        mock_client._models.execute_kw.side_effect = [
            {"closed": True, "stranded_count": 1,
             "stranded_lines": [{"id": 1, "state": "picked_up"}],
             "summary": "Session PS-1 closed — 1 line(s) left off-shelf."},
            [{"id": 1, "name": "PS-1", "state": "closed"}],   # self.get()
        ]
        result = photo.close_session(1, force=True)
        assert result["closed"] is True
        assert result["stranded_count"] == 1
        assert result["session"]["name"] == "PS-1"
        _, _, _, kwargs = _calls(mock_client)[0]
        assert kwargs == {"force": True}

    def test_close_session_closes_cleanly_via_guard(self, photo, mock_client):
        mock_client._models.execute_kw.side_effect = [
            {"closed": True, "stranded_count": 0, "stranded_lines": [],
             "summary": "Session PS-1 closed."},
            [{"id": 1, "name": "PS-1", "state": "closed"}],   # self.get()
        ]
        result = photo.close_session(1)
        assert result["closed"] is True
        assert result["stranded_count"] == 0

    def test_close_session_reraises_a_real_fault(self, photo, mock_client):
        """A non-missing-method fault (e.g. access denied) must propagate, not
        be swallowed into the advisory fallback."""
        import xmlrpc.client
        mock_client._models.execute_kw.side_effect = xmlrpc.client.Fault(
            2, "odoo.exceptions.AccessError: not allowed on photo.session"
        )
        with pytest.raises(OdooError):
            photo.close_session(1)

    # ── Fallback path: advisory guard on an un-upgraded database ──────

    def test_close_session_falls_back_when_guard_absent(self, photo, mock_client):
        """When the module lacks action_end_guarded, the advisory guard runs
        and still refuses to strand stock."""
        mock_client._models.execute_kw.side_effect = [
            _missing_method_fault(),                              # action_end_guarded
            1,                                                    # search_count
            [{"id": 1, "state": "picked_up", "product_id": [5, "Laptop"]}],
        ]
        result = photo.close_session(1)
        assert result["closed"] is False
        assert result["stranded_count"] == 1
        assert "action_end" not in [c[1] for c in _calls(mock_client)]

    def test_fallback_counts_stranded_beyond_the_sample_page(
        self, photo, mock_client
    ):
        """The advisory guard counts server-side, not by measuring the page it
        fetched — a session with more lines than the sample must not hide a
        stranded one."""
        mock_client._models.execute_kw.side_effect = [
            _missing_method_fault(),                              # action_end_guarded
            5000,                                                 # search_count
            [{"id": 1, "state": "picked_up", "product_id": [5, "Laptop"]}],
        ]
        result = photo.close_session(1)
        assert result["closed"] is False
        assert result["stranded_count"] == 5000, (
            "count must come from search_count, not len() of the sample"
        )
        counts = [c for c in _calls(mock_client) if c[1] == "search_count"]
        assert counts, "expected a server-side search_count"
        domain = counts[0][2][0]
        assert ["session_id", "=", 1] in domain
        assert any(c[0] == "state" and c[1] == "in" for c in domain
                   if isinstance(c, list))

    def test_fallback_rechecks_immediately_before_closing(
        self, photo, mock_client
    ):
        """The advisory guard re-checks right before acting, narrowing (not
        removing) the race window."""
        mock_client._models.execute_kw.side_effect = [
            _missing_method_fault(),                             # action_end_guarded
            0,      # initial count: nothing off-shelf
            1,      # recheck: a line went off-shelf in the meantime
            [{"id": 1, "state": "picked_up", "product_id": [5, "Laptop"]}],
        ]
        result = photo.close_session(1)
        assert result["closed"] is False
        assert result["stranded_count"] == 1
        assert "went off the shelf while this call was in flight" in result["summary"]
        assert "action_end" not in [c[1] for c in _calls(mock_client)]

    def test_fallback_closes_when_nothing_is_off_shelf(self, photo, mock_client):
        mock_client._models.execute_kw.side_effect = [
            _missing_method_fault(),                             # action_end_guarded
            0,      # initial count
            0,      # recheck
            True,   # action_end
            [{"id": 1, "name": "PS-1", "state": "closed"}],
        ]
        result = photo.close_session(1)
        assert result["closed"] is True
        assert result["stranded_count"] == 0

    def test_fallback_force_closes_and_still_reports(
        self, photo, mock_client
    ):
        mock_client._models.execute_kw.side_effect = [
            _missing_method_fault(),                             # action_end_guarded
            1,                                                    # search_count
            [{"id": 1, "state": "picked_up", "product_id": [5, "Laptop"]}],
            True,                                                 # action_end
            [{"id": 1, "name": "PS-1", "state": "closed"}],
        ]
        result = photo.close_session(1, force=True)
        assert result["closed"] is True
        assert result["stranded_count"] == 1

    def test_required_groups_match_the_module(self, photo):
        assert photo.REQUIRED_GROUPS == (
            "product_photography.group_photo_user",
            "product_photography.group_photo_manager",
        )


# ── Field filtering against database drift ───────────────────────────


class TestFieldFiltering:
    """BaseOps intersects declared field lists with what the database has.

    Optional modules add fields to shared models (`helpdesk_repair` puts
    `ticket_id` on repair.order; `atech_messaging` puts `sms_fsm_*` on
    project.task), and those modules are installed on staging but not
    production. Odoo's read() rejects an unknown field outright, so one such
    name breaks every get() on that namespace while the list and summary
    methods — which use LIST_FIELDS — keep working.
    """

    def test_fields_absent_from_the_database_are_dropped(self, fb, mock_client):
        fb._model_field_cache = {"id", "name", "state"}
        effective = fb._fields(detail=True)
        assert set(effective) <= {"id", "name", "state"}
        assert "id" in effective and "name" in effective

    def test_fields_present_in_the_database_are_kept(self, fb, mock_client):
        fb._model_field_cache = set(fb.DETAIL_FIELDS)
        assert fb._fields(detail=True) == fb.DETAIL_FIELDS

    def test_unknown_schema_disables_filtering(self, fb, mock_client):
        """Empty cache means 'schema unknown' — pass declarations through."""
        fb._model_field_cache = set()
        assert fb._fields(detail=True) == fb.DETAIL_FIELDS

    def test_never_returns_an_empty_field_list(self, fb, mock_client):
        """Odoo reads ALL fields when `fields` is empty.

        Filtering down to nothing would silently turn a narrow read into a
        full one, so a total mismatch falls back to the declaration and lets
        the read raise instead.
        """
        fb._model_field_cache = {"totally", "different", "model"}
        effective = fb._fields(detail=True)
        assert effective == fb.DETAIL_FIELDS
        assert effective, "must never hand Odoo an empty field list"

    def test_explicit_caller_fields_are_not_filtered(self, fb, mock_client):
        """A typo in an explicit fields= should error, not be swallowed."""
        fb._model_field_cache = {"id", "name"}
        mock_client._models.execute_kw.return_value = []
        fb.search([], fields=["id", "name", "definitely_not_a_field"])
        _, _, _, odoo_kwargs = _calls(mock_client)[0]
        assert "definitely_not_a_field" in odoo_kwargs["fields"]
