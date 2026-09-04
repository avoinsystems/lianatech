from odoo import api, fields, models, _
from odoo.exceptions import UserError

import requests
import json
import hashlib
import hmac
import datetime
import time
from urllib.parse import urlencode

import pytz

# --- General variables ---
BASE_PATH    = 'rest'
# Base path segment for the LianaMailer REST API. Unlike Automation (``rest``)
# the Mailer API is served under ``/api`` and V1 request bodies are positional
# JSON arrays rather than objects.
MAILER_BASE_PATH = 'api'
CONTENT_TYPE = 'application/json'
METHOD       = 'POST'
# POST body: Liana Automation event/tracking payload (channel, no_duplicates, data, ...).
AUTOMATION_API_EVENT_PATH = "v1/import"
# POST body: empty object. Returns list of channels available on the account.
AUTOMATION_API_CHANNEL_LIST_PATH = "v1/channel/list"
# POST body: empty array. Returns the account's customer properties (field catalog).
MAILER_API_PROPERTIES_PATH = "v1/getCustomerProperties"
# POST body: keyed object with the list name/id, base64 CSV payload and folder.
# This is the only endpoint accepting a folder, both when creating a list and
# when moving an existing one.
MAILER_API_IMPORT_LIST_PATH = "v2/import/mailinglist"
# GET with URL parameters only. Returns recipient tracking events.
MAILER_API_EVENTS_PATH = "v3/events"

# Event types fetched from the Mailer ``events`` endpoint. The endpoint accepts
# a single type per request, so one paged request loop runs per type.
MAILER_EVENT_TYPE_OPEN = "mailopen"
MAILER_EVENT_TYPE_CLICK = "maillink"
MAILER_EVENT_TYPE_SUBSCRIBE = "list-join"
MAILER_FETCHED_EVENT_TYPES = (
    MAILER_EVENT_TYPE_OPEN,
    MAILER_EVENT_TYPE_CLICK,
    MAILER_EVENT_TYPE_SUBSCRIBE,
)
# Events are paged; the API documentation recommends pages of 100.
MAILER_EVENT_PAGE_SIZE = 100
# The API allows four requests per second at most.
MAILER_REQUEST_INTERVAL = 0.25
# Timezone the ``events`` endpoint expects ``at_start``/``at_end`` in.
MAILER_EVENT_TIMEZONE = "Europe/Helsinki"
# Events are re-fetched from slightly before the last run to catch events
# registered late by Liana. Duplicates are dropped on import.
MAILER_EVENT_FETCH_OVERLAP = datetime.timedelta(hours=1)
# Period covered by the very first fetch of a backend.
MAILER_EVENT_INITIAL_LOOKBACK = datetime.timedelta(days=7)
# Partner field carrying the Liana identity, matched against the Mailer
# property it is mapped to.
LIANA_EXTRA1_FIELD = "liana_extra1"

# Property handles reserved by LianaMailer; they must not be sent as custom
# properties in import payloads (see the API "Restrictions" documentation).
MAILER_RESERVED_PROPERTY_NAMES = frozenset({
    "admin", "archive", "delivery_id", "email", "id", "ip", "list_id",
    "list_name", "mail_id", "origin", "reason", "recipient_email",
    "recipient_sms", "scheduled", "sms", "subject", "tracking_id", "url",
    "useragent",
})

# XML ids of the default Liana server actions shipped in
# ``data/default_automations.xml``. Used by
# :meth:`LianaBackend.action_install_default_automations`.
DEFAULT_AUTOMATION_ACTION_XMLIDS = (
    "connector_liana.ir_actions_server_crm_lead_stage_changed",
    "connector_liana.ir_actions_server_sale_order_state_changed",
)


class LianaError(Exception):
    pass


class LianaBackend(models.Model):
    _name = "liana.backend"
    _description = "Liana Integration Backend Configuration"

    name = fields.Char(required=True)

    liana_automation_address = fields.Char(
        string="Automation Address",
    )
    
    liana_automation_secret = fields.Char(
        string="Automation Secret",
        copy=False,
    )

    liana_automation_user = fields.Char(
        string="Automation User",
        copy=False,
    )

    liana_automation_realm = fields.Char(
        string="Automation Realm",
    )

    liana_mailer_address = fields.Char(
        string="Mailer Address",
        help="Base URL of the LianaMailer REST API, e.g. https://rest.lianamailer.com",
    )

    liana_mailer_secret = fields.Char(
        string="Mailer Secret",
        copy=False,
    )

    liana_mailer_user = fields.Char(
        string="Mailer User",
        copy=False,
    )

    liana_mailer_realm = fields.Char(
        string="Mailer Realm",
    )

    mailing_list_folder = fields.Char(
        string="Mailing List Folder",
        help="Default Liana Mailer folder path for lists exported through this "
             "backend. Multi-level paths are supported and created on demand.",
        default="Odoo",
    )

    mailer_event_fetch_date = fields.Datetime(
        string="Events Fetched Until",
        readonly=True,
        copy=False,
        help="Start of the period covered by the next Liana Mailer event fetch. "
             "Set automatically after each successful fetch.",
    )

    mailer_extra1_property_id = fields.Many2one(
        comodel_name="liana.property",
        string="Liana ID Property",
        compute="_compute_mailer_extra1_property_id",
        help="Liana Mailer property the contacts' Liana ID is exported to, "
             "taken from the field mappings below. When set, mailer events are "
             "matched to contacts by this property instead of the email address.",
    )

    property_ids = fields.One2many(
        comodel_name="liana.property",
        inverse_name="backend_id",
        string="Liana Properties",
    )

    mapping_ids = fields.One2many(
        comodel_name="liana.field.mapping",
        inverse_name="backend_id",
        string="Field Mappings",
    )

    liana_channel_id = fields.Many2one(
        comodel_name="liana.channel",
        string="Default Liana Channel",
        domain="[('backend_id', '=', id), ('system_name', 'not in', ('system', 'esp'))]",
    )

    def _send_signed_request(
        self, base_path, path, data, address, secret, user, realm, method=METHOD,
    ):
        """Send an HMAC-signed request to a Liana REST API and return the JSON body.

        The signing scheme (SHA256 HMAC over method/md5/content-type/date/body/
        path) is shared by both the Automation and Mailer APIs; only the base
        path segment, credentials and body encoding differ.

        V3 Mailer endpoints are GET-only and take their parameters in the URL,
        so ``path`` carries the query string and the signed body is empty.
        """
        self.ensure_one()

        body = "" if method == "GET" else json.dumps(data)

        # ISO 8601 date; PHP's 'c' format is roughly equivalent to isoformat().
        date_str = datetime.datetime.now().astimezone().isoformat(timespec='seconds')

        content_md5 = hashlib.md5(body.encode('utf-8')).hexdigest()

        # Signature content order: Method, MD5, Content-Type, Date, Body, Path.
        # The path must include the query string exactly as requested.
        full_api_path = f"/{base_path}/{path}"
        signature_payload = "\n".join([
            method,
            content_md5,
            CONTENT_TYPE,
            date_str,
            body,
            full_api_path,
        ])

        signature = hmac.new(
            (secret or "").encode('utf-8'),
            signature_payload.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

        auth_header = f"{realm} {user}:{signature}"

        headers = {
            "Authorization": auth_header,
            "Date": date_str,
            "Content-MD5": content_md5,
            "Content-Type": CONTENT_TYPE,
        }

        full_url = f"{address}/{base_path}/{path}"

        try:
            send = requests.get if method == "GET" else requests.post
            response = send(full_url, data=body, headers=headers)
            response.raise_for_status()  # Raises an error for 4xx or 5xx responses
            return response.json()
        except requests.exceptions.RequestException as e:
            raise LianaError(f"API request failed: {str(e)}")

    def automation_send_api_request(self, path, data):
        """Sends an authenticated API request to Liana Automation."""
        return self._send_signed_request(
            BASE_PATH,
            path,
            data,
            self.liana_automation_address,
            self.liana_automation_secret,
            self.liana_automation_user,
            self.liana_automation_realm,
        )

    def mailer_send_api_request(self, path, params):
        """Send an authenticated request to the LianaMailer REST API.

        V1 endpoints identify parameters positionally, so ``params`` is a list
        there (empty for endpoints taking no parameters). V2 endpoints identify
        them by key, so ``params`` is a dict.
        """
        return self._send_signed_request(
            MAILER_BASE_PATH,
            path,
            params,
            self.liana_mailer_address,
            self.liana_mailer_secret,
            self.liana_mailer_user,
            self.liana_mailer_realm,
        )

    def mailer_get_api_request(self, path, params):
        """Send an authenticated GET request to the LianaMailer REST API.

        V3 endpoints take all their parameters in the URL. The query string is
        built once and signed as part of the path, so it has to reach the
        request unchanged.
        """
        query = urlencode(params or {})
        return self._send_signed_request(
            MAILER_BASE_PATH,
            f"{path}?{query}" if query else path,
            None,
            self.liana_mailer_address,
            self.liana_mailer_secret,
            self.liana_mailer_user,
            self.liana_mailer_realm,
            method="GET",
        )

    def _check_automation_settings(self):
        if any((
            not self.liana_automation_address,
            not self.liana_automation_secret,
            not self.liana_automation_user,
            not self.liana_automation_realm,
        )):
            raise UserError("Please fill in all Liana Automation settings to test the connection.")

    def _has_mailer_settings(self):
        self.ensure_one()
        return all((
            self.liana_mailer_address,
            self.liana_mailer_secret,
            self.liana_mailer_user,
            self.liana_mailer_realm,
        ))

    def _check_mailer_settings(self):
        if not self._has_mailer_settings():
            raise UserError(_("Please fill in all Liana Mailer settings first."))

    def test_mailer_connection(self):
        """Test connection to the LianaMailer API using the ``echoMessage`` endpoint."""
        self.ensure_one()
        self._check_mailer_settings()

        response = self.mailer_send_api_request("v1/echoMessage", ["hello"])
        if isinstance(response, dict) and response.get("result") == "hello":
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Success"),
                    "message": _("Liana Mailer connection successful!"),
                    "type": "success",
                    "sticky": False,
                },
            }
        raise LianaError(f"Unexpected response from Liana Mailer: {response!r}")

    def fetch_properties(self):
        """Call ``getCustomerProperties`` and upsert results into ``liana.property``.

        Returns the recordset of properties present for this backend after sync.
        """
        self.ensure_one()
        self._check_mailer_settings()

        response = self.mailer_send_api_request(MAILER_API_PROPERTIES_PATH, [])
        # Successful responses wrap the payload in a ``result`` list.
        if not isinstance(response, dict) or "result" not in response:
            raise LianaError(
                f"Unexpected response from Liana Mailer getCustomerProperties: {response!r}"
            )
        items = response.get("result")
        if not isinstance(items, list):
            raise LianaError(
                f"Unexpected response from Liana Mailer getCustomerProperties: {response!r}"
            )
        return self.env["liana.property"].sudo()._update_from_api(self, items)

    def action_fetch_liana_properties(self):
        """Refresh ``liana.property`` from the LianaMailer ``getCustomerProperties`` endpoint."""
        self.ensure_one()
        try:
            properties = self.fetch_properties()
        except LianaError as err:
            raise UserError(str(err)) from err
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Liana Properties"),
                "message": _("Fetched %s propert(y/ies) from Liana Mailer.") % len(properties),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    @api.depends("mapping_ids.partner_field_id", "mapping_ids.liana_property_id")
    def _compute_mailer_extra1_property_id(self):
        for backend in self:
            backend.mailer_extra1_property_id = backend._get_extra1_property()

    def _get_extra1_property(self):
        """Return the Mailer property the contacts' Liana ID is exported to."""
        self.ensure_one()
        mapping = self.mapping_ids.filtered(
            lambda m: m.partner_field_id.name == LIANA_EXTRA1_FIELD
        )
        return mapping[:1].liana_property_id

    def _get_mailer_event_types(self):
        """Return the Mailer event types fetched by this backend."""
        return MAILER_FETCHED_EVENT_TYPES

    def _mailer_event_datetime_param(self, value):
        """Format a naive UTC datetime as the local time the API expects."""
        local = pytz.utc.localize(value).astimezone(
            pytz.timezone(MAILER_EVENT_TIMEZONE)
        )
        return local.replace(tzinfo=None).isoformat(timespec="seconds")

    def _mailer_event_params(self, event_type, date_from, date_to, offset):
        """Build the query parameters of one ``events`` request."""
        self.ensure_one()
        params = {
            "type": event_type,
            "at_start": self._mailer_event_datetime_param(date_from),
            "at_end": self._mailer_event_datetime_param(date_to),
            "limit": MAILER_EVENT_PAGE_SIZE,
            "offset": offset,
            "sort": "at",
            "list_data": "true",
        }
        extra1_property = self._get_extra1_property()
        if extra1_property:
            # Recipient properties are omitted from the response unless asked
            # for by name.
            params["properties[0]"] = extra1_property.name
        return params

    def _fetch_mailer_event_page(self, event_type, date_from, date_to, offset):
        """Return one page of events, validating the response envelope."""
        self.ensure_one()
        response = self.mailer_get_api_request(
            MAILER_API_EVENTS_PATH,
            self._mailer_event_params(event_type, date_from, date_to, offset),
        )
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise LianaError(
                f"Unexpected response from Liana Mailer "
                f"{MAILER_API_EVENTS_PATH}: {response!r}"
            )
        return items

    def fetch_mailer_events(self, date_from=None, date_to=None):
        """Import Liana Mailer events into ``liana.mailer.event``.

        One paged request loop runs per event type since the endpoint accepts a
        single type at a time. Without an explicit period the events since the
        previous fetch are imported and the high-water mark is moved forward;
        an explicit period leaves it untouched.
        """
        self.ensure_one()
        self._check_mailer_settings()

        started_at = fields.Datetime.now()
        explicit_period = bool(date_to)
        date_to = date_to or started_at
        date_from = date_from or self.mailer_event_fetch_date or (
            date_to - MAILER_EVENT_INITIAL_LOOKBACK
        )

        events = self.env["liana.mailer.event"]
        for event_type in self._get_mailer_event_types():
            offset = 0
            while True:
                if offset:
                    # Stay below the four requests per second the API allows.
                    time.sleep(MAILER_REQUEST_INTERVAL)
                items = self._fetch_mailer_event_page(
                    event_type, date_from, date_to, offset,
                )
                events |= events._import_events(self, items)
                if len(items) < MAILER_EVENT_PAGE_SIZE:
                    break
                offset += MAILER_EVENT_PAGE_SIZE

        events._relink_unmatched(self)
        if not explicit_period:
            self.mailer_event_fetch_date = started_at - MAILER_EVENT_FETCH_OVERLAP
        return events

    def action_fetch_mailer_events(self):
        """Import Liana Mailer events and report how many were new."""
        self.ensure_one()
        try:
            events = self.fetch_mailer_events()
        except LianaError as err:
            raise UserError(str(err)) from err
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Liana Mailer Events"),
                "message": _("Imported %s new event(s) from Liana Mailer.") % len(events),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_fetch_liana_channels(self):
        """Refresh `liana.channel` from the Liana Automation `channel/list` endpoint."""
        self.ensure_one()
        try:
            channels = self.fetch_channels()
        except LianaError as err:
            raise UserError(str(err)) from err
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Liana Channels"),
                "message": _("Fetched %s channel(s) from Liana Automation.") % len(channels),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def test_automation_connection(self):
        """Test connection to Liana Automation API using pingpong endpoint.
        
        Shows a notification message to the user on success.
                
        Raises:
            UserError: If connection fails or settings are missing
            LianaError: If API response is unexpected
            
        Returns:
            dict: Client action to display success notification
        """
        self._check_automation_settings()
        
        response = self.automation_send_api_request('v1/pingpong', {"ping": "pong"})
        expected_response = {"pong": "pong"}
        if response == expected_response:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Success',
                    'message': 'Liana Automation connection successful!',
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            raise LianaError(f"Unexpected response from Liana Automation: {response}")

    def fetch_channels(self):
        """Call channel/list and upsert results into liana.channel.

        Returns the recordset of channels present for this backend after sync.
        """
        self.ensure_one()
        self._check_automation_settings()

        response = self.automation_send_api_request(
            AUTOMATION_API_CHANNEL_LIST_PATH, {}
        )
        # Automation may return HTTP 200 with an auth-failure envelope.
        if isinstance(response, dict) and "message" in response:
            raise LianaError(
                f"Liana Automation channel/list failed: {response.get('message')}"
            )
        if not isinstance(response, list):
            raise LianaError(
                f"Unexpected response from Liana Automation channel/list: {response!r}"
            )
        return self.env["liana.channel"].sudo()._update_from_api(self, response)

    def action_install_default_automations(self):
        """Stamp this backend (+ its default channel) onto the shipped Liana
        automation templates and activate them.

        The two template rules are created (inactive, with no backend/channel)
        by ``data/default_automations.xml``. This action makes them runnable
        by filling in the missing backend/channel references and flipping the
        parent ``base.automation`` records to active.

        Re-running the action is safe: it overwrites the same fields on the
        same records (looked up by XML id).
        """
        self.ensure_one()
        if not self.liana_channel_id:
            raise UserError(_(
                "Pick a default Liana channel before installing the default "
                "automations."
            ))
        actions = self.env["ir.actions.server"]
        missing = []
        for xmlid in DEFAULT_AUTOMATION_ACTION_XMLIDS:
            action = self.env.ref(xmlid, raise_if_not_found=False)
            if action:
                actions |= action
            else:
                missing.append(xmlid)
        if missing:
            raise UserError(_(
                "Default Liana automation templates are missing: %s. "
                "Reinstall or update the connector_liana module."
            ) % ", ".join(missing))

        actions.write({
            "liana_backend_id": self.id,
            "liana_channel_id": self.liana_channel_id.id,
        })
        actions.mapped("base_automation_id").write({"active": True})

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Liana Automations"),
                "message": _(
                    "Installed %s default Liana automation(s) using channel %s."
                ) % (len(actions), self.liana_channel_id.display_name),
                "type": "success",
                "sticky": False,
            },
        }

    def _build_recipient_values(self, partner):
        """Build the LianaMailer recipient dict for a ``res.partner``.

        ``email`` is always taken from the partner; the configured field
        mappings add custom property values keyed by their Liana property name.
        Reserved property names are skipped defensively.
        """
        self.ensure_one()
        values = {"email": partner.email}
        for mapping in self.mapping_ids:
            property_name = mapping.liana_property_id.name
            field_name = mapping.partner_field_id.name
            if not property_name or not field_name:
                continue
            if property_name in MAILER_RESERVED_PROPERTY_NAMES:
                continue
            raw = partner[field_name]
            if raw is False or raw is None:
                raw = ""
            elif not isinstance(raw, (str, int, float)):
                raw = str(raw)
            values[property_name] = raw
        return values

    @api.model
    def _get_default_backend(self):
        """Return the unique backend in the database, or empty recordset.

        Used as the implicit fallback when a Liana action does not pin a
        specific backend and exactly one is configured.
        """
        backends = self.sudo().search([], limit=2)
        return backends if len(backends) == 1 else self.browse()

