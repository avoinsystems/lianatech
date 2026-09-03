import json
import logging
from ast import literal_eval

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

from .liana_backend import LianaError

_logger = logging.getLogger(__name__)


class LianaMailingList(models.Model):
    _name = "liana.mailing.list"
    _description = "Liana Mailing List"
    _order = "name"

    name = fields.Char(required=True)
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
        default=lambda self: self.env["liana.backend"]._get_default_backend(),
        help="Liana backend used when exporting this list to Liana Mailer.",
    )
    liana_list_id = fields.Integer(
        string="Liana List ID",
        readonly=True,
        copy=False,
        help="Identifier of the corresponding mailing list in Liana Mailer. "
             "Set automatically on the first export.",
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
        backend = self.liana_backend_id or self.env["liana.backend"]._get_default_backend()
        if not backend:
            raise UserError(_("No Liana backend configured for this mailing list."))
        # Backend/mapping/property config is admin-only; run export with elevated
        # rights so marketing managers can trigger it.
        return backend.sudo()

    def action_export_to_liana(self):
        """Export this mailing list and its recipients to Liana Mailer."""
        self.ensure_one()
        backend = self._liana_get_backend()
        backend._check_mailer_settings()

        try:
            list_id = self._liana_ensure_list(backend)

            recipients = [
                backend._build_recipient_values(partner)
                for partner in self._get_recipients()
                if partner.email
            ]

            if self.liana_truncate:
                self._liana_call(
                    backend, "v1/truncateMailingList", [list_id],
                )

            self._liana_call(
                backend,
                "v1/importJSONToMailingList",
                [list_id, json.dumps(recipients)],
            )
            self.date_liana_export = fields.Datetime.now()
        except LianaError as err:
            raise UserError(str(err)) from err

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

    def _liana_ensure_list(self, backend):
        """Return the Liana list id, creating the remote list on first export."""
        self.ensure_one()
        if self.liana_list_id:
            return self.liana_list_id

        response = backend.mailer_send_api_request(
            "v1/createMailingList", [self.name, self.name or ""],
        )
        list_id = response.get("result") if isinstance(response, dict) else None
        if not list_id:
            raise LianaError(
                f"Unexpected response from Liana Mailer createMailingList: {response!r}"
            )
        self.liana_list_id = int(list_id)
        return self.liana_list_id

    def _liana_call(self, backend, path, params):
        """Call a Mailer endpoint and validate the ``succeed``/``result`` envelope."""
        response = backend.mailer_send_api_request(path, params)
        if isinstance(response, dict) and response.get("succeed") is False:
            raise LianaError(
                f"Liana Mailer {path} failed: {response.get('message') or response!r}"
            )
        return response
