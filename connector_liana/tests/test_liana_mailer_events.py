import base64
import hashlib
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.connector_liana.models import liana_backend as liana_backend_module
from odoo.addons.connector_liana.models.liana_backend import (
    MAILER_API_EVENTS_PATH,
    MAILER_EVENT_PAGE_SIZE,
    MAILER_EVENT_TYPE_CLICK,
    MAILER_EVENT_TYPE_OPEN,
    MAILER_EVENT_TYPE_SUBSCRIBE,
)

EXTRA1_PROPERTY_NAME = "odoo_id"


class LianaMailerEventCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = cls.env["liana.backend"].create({
            "name": "Mailer backend",
            "integration_type": "mailer",
            "liana_mailer_address": "https://rest.example.test",
            "liana_mailer_secret": "mailer-secret",
            "liana_mailer_user": "mailer-user",
            "liana_mailer_realm": "MailerRealm",
        })
        cls.partner = cls.env["res.partner"].create({
            "name": "Alice",
            "email": "Alice@example.test",
        })
        cls.events = cls.env["liana.mailer.event"]

    def _add_extra1_mapping(self, backend=None):
        """Map ``liana_extra1`` to a Liana Mailer property on the backend."""
        backend = backend or self.backend
        prop = self.env["liana.property"].create({
            "name": EXTRA1_PROPERTY_NAME,
            "handle": "42",
            "backend_id": backend.id,
        })
        extra1_field = self.env["ir.model.fields"]._get("res.partner", "liana_extra1")
        self.env["liana.field.mapping"].create({
            "backend_id": backend.id,
            "partner_field_id": extra1_field.id,
            "liana_property_id": prop.id,
        })
        return prop

    def _event(self, event_type=MAILER_EVENT_TYPE_OPEN, at="2026-03-02T14:30:00+02:00",
               email="alice@example.test", extra1=None, recipient_id=5, data=None,
               liana_list=None):
        event = {
            "at": at,
            "type": event_type,
            "data": data if data is not None else {"delivery-id": 77, "list-id": 7},
            "recipient": {"id": recipient_id, "email": email},
        }
        if extra1 is not None:
            event["recipient"]["properties"] = {EXTRA1_PROPERTY_NAME: extra1}
        if liana_list is not None:
            event["list"] = liana_list
        return event

    def _patch_events(self, pages_by_type=None, default=()):
        """Patch the events endpoint with canned pages per event type.

        ``pages_by_type`` maps an event type to the list of pages returned for
        it; types not listed return a single page of ``default``.
        """
        pages_by_type = pages_by_type or {}
        served = {}

        def _side_effect(path, params):
            self.assertEqual(path, MAILER_API_EVENTS_PATH)
            pages = pages_by_type.get(params["type"], [list(default)])
            index = served.get(params["type"], 0)
            served[params["type"]] = index + 1
            return {"items": pages[index] if index < len(pages) else []}

        return patch.object(
            liana_backend_module.LianaBackend,
            "mailer_get_api_request",
            side_effect=_side_effect,
            autospec=False,
        )

    def _params_by_type(self, mock_get):
        return {
            call.args[1]["type"]: call.args[1] for call in mock_get.call_args_list
        }


@tagged("post_install", "-at_install")
class TestLianaMailerEventRequest(LianaMailerEventCommon):

    def test_fetch_requests_one_loop_per_event_type(self):
        with self._patch_events() as mock_get:
            self.backend.fetch_mailer_events()

        self.assertEqual(
            sorted(self._params_by_type(mock_get)),
            sorted([
                MAILER_EVENT_TYPE_OPEN,
                MAILER_EVENT_TYPE_CLICK,
                MAILER_EVENT_TYPE_SUBSCRIBE,
            ]),
        )

    def test_fetch_sends_period_in_helsinki_time(self):
        # Summer time, so Helsinki is UTC+3 on this date.
        self.backend.mailer_event_fetch_date = datetime(2026, 3, 30, 12, 0, 0)
        with self._patch_events() as mock_get:
            self.backend.fetch_mailer_events(
                date_to=datetime(2026, 3, 30, 13, 0, 0),
            )

        params = self._params_by_type(mock_get)[MAILER_EVENT_TYPE_OPEN]
        self.assertEqual(params["at_start"], "2026-03-30T15:00:00")
        self.assertEqual(params["at_end"], "2026-03-30T16:00:00")

    def test_fetch_asks_for_extra1_property_when_mapped(self):
        self._add_extra1_mapping()
        with self._patch_events() as mock_get:
            self.backend.fetch_mailer_events()

        params = self._params_by_type(mock_get)[MAILER_EVENT_TYPE_OPEN]
        self.assertEqual(params["properties[0]"], EXTRA1_PROPERTY_NAME)

    def test_fetch_omits_properties_without_mapping(self):
        with self._patch_events() as mock_get:
            self.backend.fetch_mailer_events()

        params = self._params_by_type(mock_get)[MAILER_EVENT_TYPE_OPEN]
        self.assertNotIn("properties[0]", params)

    def test_fetch_pages_until_partial_page(self):
        first_page = [
            self._event(recipient_id=index) for index in range(MAILER_EVENT_PAGE_SIZE)
        ]
        second_page = [self._event(recipient_id=1000)]
        pages = {MAILER_EVENT_TYPE_OPEN: [first_page, second_page]}
        with self._patch_events(pages_by_type=pages) as mock_get:
            events = self.backend.fetch_mailer_events()

        offsets = [
            call.args[1]["offset"] for call in mock_get.call_args_list
            if call.args[1]["type"] == MAILER_EVENT_TYPE_OPEN
        ]
        self.assertEqual(offsets, [0, MAILER_EVENT_PAGE_SIZE])
        self.assertEqual(len(events), MAILER_EVENT_PAGE_SIZE + 1)

    def test_fetch_rejects_unexpected_response(self):
        with patch.object(
            liana_backend_module.LianaBackend,
            "mailer_get_api_request",
            return_value={"message": "Unauthorized"},
        ):
            with self.assertRaises(liana_backend_module.LianaError) as cm:
                self.backend.fetch_mailer_events()
        self.assertIn(MAILER_API_EVENTS_PATH, str(cm.exception))

    def test_fetch_requires_mailer_settings(self):
        backend = self.env["liana.backend"].create({
            "name": "Incomplete",
            "integration_type": "mailer",
        })
        with self.assertRaises(UserError) as cm:
            backend.fetch_mailer_events()
        self.assertIn("Liana Mailer settings", str(cm.exception))

    @patch.object(liana_backend_module.requests, "get")
    def test_get_request_signs_empty_body_and_query(self, mock_get):
        mock_get.return_value.json.return_value = {"items": []}
        mock_get.return_value.raise_for_status.return_value = None

        self.backend.mailer_get_api_request(
            MAILER_API_EVENTS_PATH, {"type": MAILER_EVENT_TYPE_OPEN},
        )

        url = mock_get.call_args.args[0]
        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(
            url,
            "https://rest.example.test/api/v3/events?type=mailopen",
        )
        # GET requests are signed over an empty body.
        self.assertFalse(mock_get.call_args.kwargs["data"])
        self.assertEqual(
            headers["Content-MD5"], hashlib.md5(b"").hexdigest(),
        )
        expected_signature = liana_backend_module.hmac.new(
            b"mailer-secret",
            "\n".join([
                "GET",
                hashlib.md5(b"").hexdigest(),
                liana_backend_module.CONTENT_TYPE,
                headers["Date"],
                "",
                "/api/v3/events?type=mailopen",
            ]).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            headers["Authorization"],
            f"MailerRealm mailer-user:{expected_signature}",
        )
        self.assertEqual(
            parse_qs(urlparse(url).query)["type"], [MAILER_EVENT_TYPE_OPEN],
        )


@tagged("post_install", "-at_install")
class TestLianaMailerEventImport(LianaMailerEventCommon):

    def test_import_stores_event_details(self):
        event = self._event(
            event_type=MAILER_EVENT_TYPE_CLICK,
            data={"delivery-id": 77, "url": "https://example.test/offer"},
            liana_list={"id": 7, "name": "Newsletter"},
        )
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_CLICK: [[event]]}):
            self.backend.fetch_mailer_events()

        stored = self.events.search([("event_type", "=", MAILER_EVENT_TYPE_CLICK)])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored.backend_id, self.backend)
        self.assertEqual(stored.partner_id, self.partner)
        self.assertEqual(stored.recipient_email, "alice@example.test")
        self.assertEqual(stored.recipient_liana_id, 5)
        self.assertEqual(stored.delivery_id, 77)
        self.assertEqual(stored.url, "https://example.test/offer")
        self.assertEqual(stored.liana_list_id, 7)
        self.assertEqual(stored.liana_list_name, "Newsletter")
        self.assertEqual(stored.payload["type"], MAILER_EVENT_TYPE_CLICK)

    def test_import_converts_event_date_to_utc(self):
        event = self._event(at="2026-03-02T14:30:00+02:00")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            self.backend.fetch_mailer_events()

        stored = self.events.search([("event_type", "=", MAILER_EVENT_TYPE_OPEN)])
        self.assertEqual(
            stored.event_date, datetime(2026, 3, 2, 12, 30, 0),
        )

    def test_import_reads_date_without_offset_as_helsinki_time(self):
        event = self._event(at="2026-03-02T14:30:00")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            self.backend.fetch_mailer_events()

        stored = self.events.search([("event_type", "=", MAILER_EVENT_TYPE_OPEN)])
        self.assertEqual(stored.event_date, datetime(2026, 3, 2, 12, 30, 0))

    def test_import_links_mailing_list(self):
        mailing_list = self.env["liana.mailing.list"].create({
            "name": "Newsletter",
            "liana_backend_id": self.backend.id,
        })
        mailing_list.liana_list_id = 7
        event = self._event(liana_list={"id": 7, "name": "Newsletter"})
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            self.backend.fetch_mailer_events()

        stored = self.events.search([("event_type", "=", MAILER_EVENT_TYPE_OPEN)])
        self.assertEqual(stored.mailing_list_id, mailing_list)

    def test_refetching_the_same_period_creates_no_duplicates(self):
        pages = {MAILER_EVENT_TYPE_OPEN: [[self._event()]]}
        with self._patch_events(pages_by_type=pages):
            self.backend.fetch_mailer_events()
        with self._patch_events(pages_by_type=pages):
            second_run = self.backend.fetch_mailer_events()

        self.assertFalse(second_run)
        self.assertEqual(
            self.events.search_count([("event_type", "=", MAILER_EVENT_TYPE_OPEN)]), 1,
        )

    def test_duplicates_within_one_page_are_imported_once(self):
        event = self._event()
        pages = {MAILER_EVENT_TYPE_OPEN: [[event, dict(event)]]}
        with self._patch_events(pages_by_type=pages):
            events = self.backend.fetch_mailer_events()

        self.assertEqual(len(events), 1)

    def test_events_differing_by_url_are_kept_apart(self):
        first = self._event(
            event_type=MAILER_EVENT_TYPE_CLICK,
            data={"delivery-id": 77, "url": "https://example.test/a"},
        )
        second = self._event(
            event_type=MAILER_EVENT_TYPE_CLICK,
            data={"delivery-id": 77, "url": "https://example.test/b"},
        )
        pages = {MAILER_EVENT_TYPE_CLICK: [[first, second]]}
        with self._patch_events(pages_by_type=pages):
            events = self.backend.fetch_mailer_events()

        self.assertEqual(len(events), 2)

    def test_import_skips_events_without_type_or_date(self):
        broken = [{"at": "2026-03-02T14:30:00+02:00"}, self._event(at=None)]
        pages = {MAILER_EVENT_TYPE_OPEN: [broken]}
        with self._patch_events(pages_by_type=pages):
            events = self.backend.fetch_mailer_events()

        self.assertFalse(events)

    def test_high_water_mark_moves_forward(self):
        before = fields.Datetime.now()
        with self._patch_events():
            self.backend.fetch_mailer_events()

        self.assertTrue(self.backend.mailer_event_fetch_date)
        self.assertLess(self.backend.mailer_event_fetch_date, before)
        self.assertGreater(
            self.backend.mailer_event_fetch_date, before - timedelta(hours=2),
        )

    def test_explicit_period_keeps_high_water_mark(self):
        with self._patch_events():
            self.backend.fetch_mailer_events(
                date_from=datetime(2026, 3, 1, 0, 0, 0),
                date_to=datetime(2026, 3, 2, 0, 0, 0),
            )

        self.assertFalse(self.backend.mailer_event_fetch_date)

    def test_failed_fetch_keeps_high_water_mark(self):
        with patch.object(
            liana_backend_module.LianaBackend,
            "mailer_get_api_request",
            return_value={"message": "Unauthorized"},
        ):
            with self.assertRaises(liana_backend_module.LianaError):
                self.backend.fetch_mailer_events()

        self.assertFalse(self.backend.mailer_event_fetch_date)

    def test_action_fetch_reports_imported_events(self):
        pages = {MAILER_EVENT_TYPE_OPEN: [[self._event()]]}
        with self._patch_events(pages_by_type=pages):
            result = self.backend.action_fetch_mailer_events()

        self.assertEqual(result["params"]["type"], "success")
        self.assertIn("1 new event(s)", result["params"]["message"])

    def test_action_fetch_raises_user_error(self):
        with patch.object(
            liana_backend_module.LianaBackend,
            "mailer_get_api_request",
            return_value={"message": "Unauthorized"},
        ):
            with self.assertRaises(UserError):
                self.backend.action_fetch_mailer_events()

    def test_cron_skips_backends_without_mailer_settings(self):
        incomplete = self.env["liana.backend"].create({
            "name": "No mailer creds",
            "integration_type": "mailer",
        })
        with self._patch_events() as mock_get:
            self.events._cron_fetch_mailer_events()

        self.assertTrue(mock_get.call_args_list)
        self.assertTrue(self.backend.mailer_event_fetch_date)
        self.assertFalse(incomplete.mailer_event_fetch_date)

    def test_cron_continues_after_failing_backend(self):
        broken = self.env["liana.backend"].create({
            "name": "Broken backend",
            "integration_type": "mailer",
            "liana_mailer_address": "https://rest.example.test",
            "liana_mailer_secret": "secret",
            "liana_mailer_user": "user",
            "liana_mailer_realm": "Realm",
        })

        with patch.object(
            liana_backend_module.LianaBackend,
            "mailer_get_api_request",
            autospec=True,
            side_effect=lambda backend, path, params: (
                {"message": "Unauthorized"} if backend == broken else {"items": []}
            ),
        ):
            self.events._cron_fetch_mailer_events()

        self.assertTrue(self.backend.mailer_event_fetch_date)
        self.assertFalse(broken.mailer_event_fetch_date)


@tagged("post_install", "-at_install")
class TestLianaMailerEventPartnerMatching(LianaMailerEventCommon):

    def test_match_by_email_is_case_insensitive(self):
        event = self._event(email="ALICE@example.test")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            events = self.backend.fetch_mailer_events()

        self.assertEqual(events.partner_id, self.partner)
        self.assertEqual(events.partner_match, "email")

    def test_match_by_extra1(self):
        self._add_extra1_mapping()
        self.partner.liana_extra1 = "L00000001"
        event = self._event(email="unknown@example.test", extra1="L00000001")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            events = self.backend.fetch_mailer_events()

        self.assertEqual(events.partner_id, self.partner)
        self.assertEqual(events.partner_match, "extra1")
        self.assertEqual(events.recipient_extra1, "L00000001")

    def test_extra1_wins_over_email(self):
        self._add_extra1_mapping()
        self.partner.liana_extra1 = "L00000001"
        other = self.env["res.partner"].create({
            "name": "Bob",
            "email": "bob@example.test",
        })
        # Liana still knows the old address, which now belongs to Bob.
        event = self._event(email=other.email, extra1="L00000001")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            events = self.backend.fetch_mailer_events()

        self.assertEqual(events.partner_id, self.partner)

    def test_unknown_extra1_falls_back_to_email(self):
        self._add_extra1_mapping()
        event = self._event(extra1="L99999999")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            events = self.backend.fetch_mailer_events()

        self.assertEqual(events.partner_id, self.partner)
        self.assertEqual(events.partner_match, "email")

    def test_unknown_recipient_is_stored_without_contact(self):
        event = self._event(email="nobody@example.test")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            events = self.backend.fetch_mailer_events()

        self.assertFalse(events.partner_id)
        self.assertEqual(events.partner_match, "unmatched")
        self.assertEqual(events.recipient_email, "nobody@example.test")

    def test_unmatched_event_is_linked_on_a_later_fetch(self):
        event = self._event(email="carol@example.test")
        pages = {MAILER_EVENT_TYPE_OPEN: [[event]]}
        with self._patch_events(pages_by_type=pages):
            stored = self.backend.fetch_mailer_events()
        self.assertFalse(stored.partner_id)

        carol = self.env["res.partner"].create({
            "name": "Carol",
            "email": "carol@example.test",
        })
        with self._patch_events():
            self.backend.fetch_mailer_events()

        self.assertEqual(stored.partner_id, carol)
        self.assertEqual(stored.partner_match, "email")

    def test_action_relink_partners_reports_linked_events(self):
        event = self._event(email="dave@example.test")
        with self._patch_events(pages_by_type={MAILER_EVENT_TYPE_OPEN: [[event]]}):
            stored = self.backend.fetch_mailer_events()
        self.env["res.partner"].create({
            "name": "Dave",
            "email": "dave@example.test",
        })

        result = stored.action_relink_partners()

        self.assertTrue(stored.partner_id)
        self.assertIn("1 event(s)", result["params"]["message"])


@tagged("post_install", "-at_install")
class TestLianaMailerEventPartnerSmartButton(LianaMailerEventCommon):

    def test_event_count_and_action(self):
        pages = {
            MAILER_EVENT_TYPE_OPEN: [[self._event()]],
            MAILER_EVENT_TYPE_CLICK: [[
                self._event(
                    event_type=MAILER_EVENT_TYPE_CLICK,
                    data={"url": "https://example.test/offer"},
                ),
            ]],
        }
        with self._patch_events(pages_by_type=pages):
            self.backend.fetch_mailer_events()

        self.partner.invalidate_recordset(["liana_mailer_event_count"])
        self.assertEqual(self.partner.liana_mailer_event_count, 2)

        action = self.partner.action_view_liana_mailer_events()
        self.assertEqual(action["res_model"], "liana.mailer.event")
        self.assertEqual(action["domain"], [("partner_id", "=", self.partner.id)])
        self.assertEqual(
            set(self.env["liana.mailer.event"].search(action["domain"]).ids),
            set(self.partner.liana_mailer_event_ids.ids),
        )

    def test_count_is_zero_without_events(self):
        self.assertEqual(self.partner.liana_mailer_event_count, 0)


@tagged("post_install", "-at_install")
class TestLianaMailerExtra1Export(LianaMailerEventCommon):

    def setUp(self):
        super().setUp()
        self.mailing_list = self.env["liana.mailing.list"].create({
            "name": "Newsletter",
            "liana_backend_id": self.backend.id,
            "partner_ids": [(6, 0, self.partner.ids)],
        })

    def _patch_import(self):
        return patch.object(
            liana_backend_module.LianaBackend,
            "mailer_send_api_request",
            return_value={
                "succeed": True,
                "result": {"succeed": True, "list_id": 12},
            },
            autospec=False,
        )

    def test_export_assigns_and_sends_liana_id(self):
        self._add_extra1_mapping()
        with self._patch_import() as mock_send:
            self.mailing_list.action_export_to_liana()

        self.assertTrue(self.partner.liana_extra1)
        import_call = next(
            call for call in mock_send.call_args_list
            if call.args[0] == liana_backend_module.MAILER_API_IMPORT_LIST_PATH
        )
        csv_data = base64.b64decode(import_call.args[1]["data"]).decode("utf-8")
        self.assertEqual(csv_data.splitlines()[0], '"email";"%s"' % EXTRA1_PROPERTY_NAME)
        self.assertIn(self.partner.liana_extra1, csv_data)

    def test_export_keeps_existing_liana_id(self):
        self._add_extra1_mapping()
        self.partner.liana_extra1 = "L00000123"
        with self._patch_import():
            self.mailing_list.action_export_to_liana()

        self.assertEqual(self.partner.liana_extra1, "L00000123")

    def test_export_without_mapping_leaves_liana_id_empty(self):
        with self._patch_import():
            self.mailing_list.action_export_to_liana()

        self.assertFalse(self.partner.liana_extra1)
