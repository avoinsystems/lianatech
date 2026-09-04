import hashlib
import json
import logging
from datetime import timezone

import pytz
from dateutil import parser as dateutil_parser

from odoo import _, api, fields, models

from .liana_backend import (
    MAILER_EVENT_TIMEZONE,
    MAILER_EVENT_TYPE_CLICK,
    MAILER_EVENT_TYPE_OPEN,
    MAILER_EVENT_TYPE_SUBSCRIBE,
    LianaError,
)

_logger = logging.getLogger(__name__)

# Every type the ``events`` endpoint may return, so an event type the connector
# does not fetch on its own never breaks an import.
MAILER_EVENT_TYPES = [
    (MAILER_EVENT_TYPE_OPEN, "Open"),
    (MAILER_EVENT_TYPE_CLICK, "Click"),
    (MAILER_EVENT_TYPE_SUBSCRIBE, "Subscribe"),
    ("list-quit", "Unsubscribe"),
    ("mailsent", "Sent"),
    ("mailbounce", "Bounce"),
    ("confirmed", "Confirmed"),
    ("created", "Created"),
    ("data-change", "Data Change"),
    ("deleted", "Deleted"),
    ("reactivated", "Reactivated"),
]


class LianaMailerEvent(models.Model):
    _name = "liana.mailer.event"
    _description = "Liana Mailer Event"
    _order = "event_date desc, id desc"

    backend_id = fields.Many2one(
        comodel_name="liana.backend",
        string="Liana Backend",
        required=True,
        ondelete="cascade",
        index=True,
    )
    event_type = fields.Selection(
        selection=MAILER_EVENT_TYPES,
        string="Type",
        required=True,
        index=True,
    )
    event_date = fields.Datetime(
        string="Event Date",
        required=True,
        index=True,
    )
    partner_id = fields.Many2one(
        comodel_name="res.partner",
        string="Contact",
        index=True,
        ondelete="set null",
    )
    partner_match = fields.Selection(
        selection=[
            ("extra1", "Liana ID"),
            ("email", "Email"),
            ("unmatched", "No Contact Found"),
        ],
        string="Matched By",
        required=True,
        default="unmatched",
        help="How the Liana Mailer recipient was matched to an Odoo contact.",
    )
    recipient_email = fields.Char(index=True)
    recipient_extra1 = fields.Char(
        string="Recipient Liana ID",
        index=True,
        help="Liana ID reported by Liana Mailer for the recipient of this event.",
    )
    recipient_liana_id = fields.Integer(
        string="Liana Recipient ID",
        help="Identifier of the recipient in Liana Mailer.",
    )
    liana_list_id = fields.Integer(string="Liana List ID")
    liana_list_name = fields.Char(string="Liana List")
    mailing_list_id = fields.Many2one(
        comodel_name="liana.mailing.list",
        string="Mailing List",
        ondelete="set null",
        help="Odoo mailing list exported to the Liana list of this event.",
    )
    delivery_id = fields.Integer(
        string="Liana Delivery ID",
        help="Identifier of the mailing the event originates from.",
    )
    url = fields.Char(
        string="Clicked URL",
        help="Target of the link for click events.",
    )
    reason = fields.Char()
    payload = fields.Json(help="Event as returned by the Liana Mailer API.")
    dedup_key = fields.Char(
        string="Deduplication Key",
        required=True,
        index=True,
        help="Fingerprint of the Liana Mailer event. The API does not identify "
             "events, so this is what keeps overlapping fetches from importing "
             "the same event twice.",
    )

    _sql_constraints = [
        (
            "dedup_key_unique",
            "UNIQUE(backend_id, dedup_key)",
            "A Liana Mailer event can only be imported once per backend.",
        ),
    ]

    @api.depends("event_type", "partner_id", "recipient_email", "event_date")
    def _compute_display_name(self):
        types = dict(self._fields["event_type"].selection)
        for event in self:
            recipient = event.partner_id.display_name or event.recipient_email or _("Unknown")
            event.display_name = "%s: %s" % (
                types.get(event.event_type, event.event_type), recipient,
            )

    @api.model
    def _import_events(self, backend, items):
        """Create the events of ``items`` that are not stored yet.

        Returns the created records. Events already known by their
        deduplication key are skipped, which makes re-fetching a period that
        was fetched before a no-op.
        """
        vals_list = []
        keys = set()
        for item in items:
            if not isinstance(item, dict):
                _logger.warning("Ignoring unexpected Liana Mailer event: %r", item)
                continue
            try:
                vals = self._prepare_event_vals(backend, item)
            except ValueError as err:
                _logger.warning(
                    "Ignoring unparsable Liana Mailer event %r: %s", item, err
                )
                continue
            if vals["dedup_key"] in keys:
                continue
            keys.add(vals["dedup_key"])
            vals_list.append(vals)

        if not vals_list:
            return self.browse()

        known = set(self.search([
            ("backend_id", "=", backend.id),
            ("dedup_key", "in", list(keys)),
        ]).mapped("dedup_key"))
        return self.create([
            vals for vals in vals_list if vals["dedup_key"] not in known
        ])

    @api.model
    def _prepare_event_vals(self, backend, item):
        """Build the values of one ``liana.mailer.event`` from an API event."""
        event_type = item.get("type")
        if not event_type:
            raise ValueError("event has no type")

        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        recipient = item.get("recipient") if isinstance(item.get("recipient"), dict) else {}
        liana_list = item.get("list") if isinstance(item.get("list"), dict) else {}

        email = recipient.get("email") or ""
        extra1 = self._extract_extra1(backend, recipient)
        partner, match = self._match_partner(extra1, email)

        list_id = self._to_int(liana_list.get("id") or data.get("list-id"))
        return {
            "backend_id": backend.id,
            "event_type": event_type,
            "event_date": self._parse_event_date(item.get("at")),
            "partner_id": partner.id,
            "partner_match": match,
            "recipient_email": email,
            "recipient_extra1": extra1,
            "recipient_liana_id": self._to_int(recipient.get("id")),
            "liana_list_id": list_id,
            "liana_list_name": liana_list.get("name") or "",
            "mailing_list_id": self._find_mailing_list(backend, list_id).id,
            "delivery_id": self._to_int(data.get("delivery-id") or data.get("delivery_id")),
            "url": data.get("url") or "",
            "reason": item.get("reason") or data.get("reason") or "",
            "payload": item,
            "dedup_key": self._build_dedup_key(item),
        }

    @api.model
    def _extract_extra1(self, backend, recipient):
        """Return the recipient's Liana ID from its Mailer properties."""
        extra1_property = backend._get_extra1_property()
        properties = recipient.get("properties")
        if not extra1_property or not isinstance(properties, dict):
            return ""
        value = properties.get(extra1_property.name)
        return str(value) if value else ""

    @api.model
    def _match_partner(self, extra1, email):
        """Return the contact of a Mailer recipient and how it was matched.

        The Liana ID is preferred over the email address because it keeps
        identifying the contact after either side changed the address.
        """
        partners = self.env["res.partner"]
        if extra1:
            partner = partners.search([("liana_extra1", "=", extra1)], limit=1)
            if partner:
                return partner, "extra1"
        if email:
            partner = partners.search([("email", "=ilike", email)], limit=1)
            if partner:
                return partner, "email"
        return partners, "unmatched"

    @api.model
    def _find_mailing_list(self, backend, liana_list_id):
        """Return the Odoo mailing list exported to the given Liana list."""
        if not liana_list_id:
            return self.env["liana.mailing.list"]
        return self.env["liana.mailing.list"].search([
            ("liana_list_id", "=", liana_list_id),
            ("liana_backend_id", "in", (backend.id, False)),
        ], limit=1)

    @api.model
    def _parse_event_date(self, value):
        """Return the event timestamp as a naive UTC datetime.

        Timestamps come with their offset; a timestamp without one is read in
        the timezone the endpoint works in.
        """
        if not value:
            raise ValueError("event has no date")
        parsed = dateutil_parser.isoparse(value)
        if not parsed.tzinfo:
            parsed = pytz.timezone(MAILER_EVENT_TIMEZONE).localize(parsed)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)

    @api.model
    def _build_dedup_key(self, item):
        """Return a fingerprint identifying an event across fetches.

        The API does not give events an identifier, so the raw event itself is
        hashed. Recipient properties are left out because which ones are
        returned depends on the request.
        """
        recipient = item.get("recipient") if isinstance(item.get("recipient"), dict) else {}
        fingerprint = {
            "at": item.get("at"),
            "type": item.get("type"),
            "data": item.get("data"),
            "recipient_id": recipient.get("id"),
            "recipient_email": recipient.get("email"),
        }
        canonical = json.dumps(fingerprint, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @api.model
    def _to_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _relink_unmatched(self, backend=None):
        """Re-run contact matching for stored events without a contact.

        Events can arrive before the contact exists in Odoo, or before its
        Liana ID reached Liana Mailer, so matching is retried after each fetch.
        A recipient usually has several events, so each recipient is looked up
        once and the events sharing a contact are written together.
        """
        domain = [("partner_id", "=", False)]
        if backend:
            domain.append(("backend_id", "=", backend.id))

        matches = {}
        by_partner = {}
        for event in self.search(domain):
            recipient = (event.recipient_extra1, event.recipient_email)
            if recipient not in matches:
                matches[recipient] = self._match_partner(*recipient)
            partner, match = matches[recipient]
            if partner:
                by_partner.setdefault((partner, match), self.browse())
                by_partner[(partner, match)] |= event

        relinked = self.browse()
        for (partner, match), events in by_partner.items():
            events.write({"partner_id": partner.id, "partner_match": match})
            relinked |= events
        return relinked

    def action_relink_partners(self):
        """Retry contact matching from the user interface."""
        relinked = self._relink_unmatched()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Liana Mailer Events"),
                "message": _("Linked %s event(s) to a contact.") % len(relinked),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    @api.model
    def _cron_fetch_mailer_events(self):
        """Import Liana Mailer events for every configured backend."""
        backends = self.env["liana.backend"].search([])
        for backend in backends.filtered(lambda b: b._has_mailer_settings()):
            try:
                backend.fetch_mailer_events()
            except LianaError as err:
                _logger.warning(
                    "Liana Mailer event fetch failed for backend %s (id=%s): %s",
                    backend.name, backend.id, err,
                )
