import base64
import csv
import io
import logging
from ast import literal_eval
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import SQL

from .liana_backend import (
    INTEGRATION_TYPE_MAILER,
    MAILER_API_EDIT_LIST_PATH,
    MAILER_API_IMPORT_LIST_PATH,
    LianaError,
)

_logger = logging.getLogger(__name__)


class LianaMailingList(models.Model):
    _name = "liana.mailing.list"
    _description = "Liana Mailing List"
    _order = "name"

    name = fields.Char(required=True)
    description = fields.Char(
        default="source: Odoo",
        help="Short description shown on the list in Liana Mailer. "
             "Sent on every export.",
    )
    recipient_mode = fields.Selection(
        selection=[
            ("manual", "Selected Contacts"),
            ("domain", "Search Domain"),
        ],
        string="Recipient Selection",
        required=True,
        default="manual",
        help="Selected Contacts: recipients are picked one by one.\n"
             "Search Domain: every contact matching the domain is a recipient, "
             "the domain being re-evaluated on each export.",
    )
    partner_ids = fields.Many2many(
        comodel_name="res.partner",
        relation="liana_mailing_list_res_partner_rel",
        column1="list_id",
        column2="partner_id",
        string="Recipients",
        help="Contacts exported to the corresponding Liana Mailer list.",
    )
    partner_domain = fields.Char(
        string="Recipient Domain",
        default="[]",
        help="Contacts matching this domain are exported to the corresponding "
             "Liana Mailer list.",
    )
    partner_count = fields.Integer(
        string="Recipients Count",
        compute="_compute_partner_count",
    )
    liana_backend_id = fields.Many2one(
        comodel_name="liana.backend",
        string="Liana Backend",
        domain=[("integration_type", "=", INTEGRATION_TYPE_MAILER)],
        default=lambda self: self.env["liana.backend"]._get_default_backend(
            INTEGRATION_TYPE_MAILER
        ),
        help="Liana Mailer backend used when exporting this list.",
    )
    liana_list_id = fields.Integer(
        string="Liana List ID",
        readonly=True,
        copy=False,
        help="Identifier of the corresponding mailing list in Liana Mailer. "
             "Set automatically on the first export.",
    )
    liana_folder = fields.Char(
        string="Liana Folder",
        help="Folder path in Liana Mailer, overriding the backend default. "
             "The list is moved on the next export when this changes. "
             "Use / for the root folder.",
    )
    liana_truncate = fields.Boolean(
        string="Truncate on Export",
        default=False,
        help="Empty the Liana mailing list before exporting its contacts.",
    )
    date_liana_export = fields.Datetime(
        string="Last Export",
        readonly=True,
        copy=False,
        help="Date of the last export",
    )

    @api.depends("recipient_mode", "partner_ids", "partner_domain")
    def _compute_partner_count(self):
        for mailing_list in self:
            if mailing_list.recipient_mode == "domain":
                mailing_list.partner_count = self.env["res.partner"].search_count(
                    mailing_list._get_partner_domain()
                )
            else:
                mailing_list.partner_count = len(mailing_list.partner_ids)

    @api.constrains("liana_backend_id")
    def _check_backend_integration_type(self):
        for mailing_list in self:
            backend = mailing_list.liana_backend_id
            if backend and backend.integration_type != INTEGRATION_TYPE_MAILER:
                raise ValidationError(_(
                    "Backend %(backend)s is not a Liana Mailer backend and "
                    "cannot export mailing lists.",
                    backend=backend.display_name,
                ))

    @api.constrains("recipient_mode", "partner_domain")
    def _check_partner_domain(self):
        for mailing_list in self.filtered(lambda ml: ml.recipient_mode == "domain"):
            try:
                domain = Domain(literal_eval(mailing_list.partner_domain or "[]"))
                domain.validate(self.env["res.partner"])
            except (SyntaxError, TypeError, ValueError) as err:
                raise ValidationError(_(
                    "The recipient domain of mailing list %(list_name)s is not a "
                    "valid domain on contacts: %(error)s",
                    list_name=mailing_list.name,
                    error=err,
                )) from err

    def _get_partner_domain(self):
        """Return ``partner_domain`` as a domain, matching nothing if unparsable."""
        self.ensure_one()
        try:
            return Domain(literal_eval(self.partner_domain or "[]"))
        except (SyntaxError, TypeError, ValueError):
            _logger.warning(
                "Invalid recipient domain on Liana mailing list %s (id=%s): %r",
                self.name, self.id, self.partner_domain,
            )
            return Domain.FALSE

    def _get_recipients(self):
        """Return the contacts of this list according to its recipient mode."""
        self.ensure_one()
        if self.recipient_mode == "domain":
            return self.env["res.partner"].search(self._get_partner_domain())
        return self.partner_ids

    def _match_partners(self, partners):
        """Return ``{partner_id: {list_id, ...}}`` for the lists in ``self``.

        Every list domain becomes its own SELECT restricted to ``partners``,
        and all of them are sent as a single UNION ALL statement: answering
        "which lists target this contact" then costs one round-trip whatever
        the number of lists, and each branch is anchored on the contact ids
        so nothing scales with the size of ``res.partner``.

        Lists using the manual mode are ignored; their recipients are a
        stored relation that the ORM already reads in one go.
        """
        matches = defaultdict(set)
        if not partners:
            return matches

        selects = []
        for mailing_list in self.filtered(lambda ml: ml.recipient_mode == "domain"):
            domain = Domain("id", "in", partners.ids) & mailing_list._get_partner_domain()
            if domain.is_false():
                continue
            # Keep the default active_test so the result agrees with what
            # _get_recipients() would export, and let _search apply the
            # contact record rules of the current user.
            query = self.env["res.partner"]._search(domain)
            selects.append(query.select(
                SQL("%s AS list_id", mailing_list.id),
                SQL.identifier(query.table, "id"),
            ))
        if not selects:
            return matches

        self.env.cr.execute(SQL(" UNION ALL ").join(selects))
        for list_id, partner_id in self.env.cr.fetchall():
            matches[partner_id].add(list_id)
        return matches

    def _cron_export_all_to_liana(self):
        """Export every Liana mailing list to Liana Mailer (scheduled action)."""
        for mailing_list in self.search([]):
            try:
                mailing_list.action_export_to_liana()
            except UserError as err:
                _logger.warning(
                    "Liana scheduled export failed for mailing list %s (id=%s): %s",
                    mailing_list.name, mailing_list.id, err,
                )

    def _liana_get_backend(self):
        self.ensure_one()
        backend = self.liana_backend_id or self.env["liana.backend"]._get_default_backend(
            INTEGRATION_TYPE_MAILER
        )
        if not backend:
            raise UserError(_(
                "No Liana Mailer backend configured for this mailing list."
            ))
        # Backend/mapping/property config is admin-only; run export with elevated
        # rights so marketing managers can trigger it.
        return backend.sudo()

    def _liana_get_folder(self, backend):
        """Return the Liana folder path for this list, falling back to the backend."""
        self.ensure_one()
        return self.liana_folder or backend.mailing_list_folder or ""

    def _liana_build_csv(self, recipients, backend):
        """Serialise recipient dicts as the CSV dialect expected by Liana Mailer.

        Columns are fixed by the backend field mappings rather than derived
        from the recipients, so the header stays stable across exports even
        when a property is empty for every contact of a given export.
        """
        columns = ["email"]
        for mapping in backend.mapping_ids:
            property_name = mapping.liana_property_id.name
            if property_name and property_name not in columns:
                columns.append(property_name)

        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=columns,
            delimiter=";",
            quotechar='"',
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
            extrasaction="ignore",
            restval="",
        )
        writer.writeheader()
        writer.writerows(recipients)
        return buffer.getvalue()

    def action_export_to_liana(self):
        """Export this mailing list and its recipients to Liana Mailer."""
        self.ensure_one()
        backend = self._liana_get_backend()
        backend._check_mailer_settings()

        try:
            partners = self._get_recipients().filtered("email")
            if backend._get_extra1_property():
                # The Liana ID is exported as a property, so it must exist
                # before the recipient values are built.
                partners._liana_ensure_extra1()
            recipients = [
                backend._build_recipient_values(partner) for partner in partners
            ]
            data = self._liana_build_csv(recipients, backend)

            params = {
                "name": self.name,
                "type": "csv",
                "data": base64.b64encode(data.encode("utf-8")).decode("ascii"),
                "truncate": self.liana_truncate,
            }
            if self.liana_list_id:
                params["list_id"] = self.liana_list_id
            folder = self._liana_get_folder(backend)
            if folder:
                params["folder"] = folder

            response = self._liana_call(
                backend, MAILER_API_IMPORT_LIST_PATH, params,
            )
            # The import endpoint nests its own status alongside the list id.
            result = response.get("result") if isinstance(response, dict) else None
            list_id = result.get("list_id") if isinstance(result, dict) else None
            if not list_id or result.get("succeed") is False:
                raise LianaError(
                    f"Unexpected response from Liana Mailer "
                    f"{MAILER_API_IMPORT_LIST_PATH}: {response!r}"
                )
            self.liana_list_id = int(list_id)
            self.date_liana_export = fields.Datetime.now()
        except LianaError as err:
            raise UserError(str(err)) from err

        self._liana_send_description(backend)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Liana Mailer"),
                "message": _("Exported %s recipient(s) to Liana Mailer.") % len(recipients),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def _liana_send_description(self, backend):
        """Push the list description; the import endpoint cannot carry it.

        The contacts are already imported at this point, so a failure here is
        logged rather than raised.
        """
        self.ensure_one()
        try:
            self._liana_call(
                backend,
                MAILER_API_EDIT_LIST_PATH,
                [self.liana_list_id, self.name, self.description or ""],
            )
        except LianaError as err:
            _logger.warning(
                "Could not update the Liana description of mailing list %s (id=%s): %s",
                self.name, self.id, err,
            )

    def _liana_call(self, backend, path, params):
        """Call a Mailer endpoint and validate the ``succeed``/``result`` envelope."""
        response = backend.mailer_send_api_request(path, params)
        if isinstance(response, dict) and response.get("succeed") is False:
            raise LianaError(
                f"Liana Mailer {path} failed: {response.get('message') or response!r}"
            )
        return response
