import json
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from .liana_backend import LianaError

_logger = logging.getLogger(__name__)


class MailingList(models.Model):
    _inherit = "mailing.list"

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

    def _cron_export_all_to_liana(self):
        """Export every mailing list to Liana Mailer (scheduled action)."""
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
        """Export this mailing list and its contacts to Liana Mailer."""
        self.ensure_one()
        backend = self._liana_get_backend()
        backend._check_mailer_settings()

        try:
            list_id = self._liana_ensure_list(backend)

            recipients = [
                backend._build_recipient_values(contact)
                for contact in self.contact_ids
                if contact.email
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
