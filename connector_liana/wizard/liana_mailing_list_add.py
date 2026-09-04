import logging
from ast import literal_eval

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)


class LianaMailingListAdd(models.TransientModel):
    _name = "liana.mailing.list.add"
    _description = "Add Contacts to a Liana Mailing List"

    target = fields.Selection(
        selection=[
            ("new", "New List"),
            ("existing", "Existing List"),
        ],
        string="Mailing List",
        required=True,
        default="new",
    )
    mailing_list_id = fields.Many2one(
        comodel_name="liana.mailing.list",
        string="Existing List",
    )
    name = fields.Char(string="List Name")
    liana_backend_id = fields.Many2one(
        comodel_name="liana.backend",
        string="Liana Backend",
        default=lambda self: self.env["liana.backend"]._get_default_backend(),
    )
    recipient_mode = fields.Selection(
        selection=[
            ("manual", "Selected Contacts"),
            ("domain", "Search Domain"),
        ],
        string="Recipient Selection",
        required=True,
        default="manual",
        help="Selected Contacts: the contacts ticked in the list are the "
             "recipients.\n"
             "Search Domain: every contact matching the domain is a recipient, "
             "the domain being re-evaluated on each export.",
    )
    partner_ids = fields.Many2many(
        comodel_name="res.partner",
        string="Selected Contacts",
    )
    view_domain = fields.Char(
        string="Contacts View Domain",
        readonly=True,
        help="Search domain that was active in the contacts list.",
    )
    existing_partner_domain = fields.Char(
        related="mailing_list_id.partner_domain",
        string="Current List Domain",
        readonly=True,
    )
    partner_domain = fields.Char(
        string="Recipient Domain",
        compute="_compute_partner_domain",
        store=True,
        readonly=False,
    )
    partner_count = fields.Integer(
        string="Recipients After Saving",
        compute="_compute_partner_count",
    )

    @api.model
    def default_get(self, fields_list):
        result = super().default_get(fields_list)
        context = self.env.context
        if context.get("active_model") != "res.partner":
            return result
        if "partner_ids" in fields_list:
            result["partner_ids"] = [Command.set(context.get("active_ids") or [])]
        if "view_domain" in fields_list:
            result["view_domain"] = repr(context.get("active_domain") or [])
        return result

    @api.onchange("target", "mailing_list_id")
    def _onchange_target(self):
        # The mode of an existing list is not up for negotiation here: switching
        # it would silently drop the recipients the list already has.
        if self.target == "existing" and self.mailing_list_id:
            self.recipient_mode = self.mailing_list_id.recipient_mode

    @api.depends(
        "target", "mailing_list_id", "mailing_list_id.recipient_mode", "view_domain",
    )
    def _compute_partner_domain(self):
        for wizard in self:
            domain = wizard._get_view_domain()
            mailing_list = wizard.mailing_list_id
            if wizard.target == "existing" and mailing_list.recipient_mode == "domain":
                domain |= mailing_list._get_partner_domain()
            wizard.partner_domain = self._domain_to_char(domain)

    @api.depends(
        "target", "mailing_list_id", "recipient_mode", "partner_ids", "partner_domain",
    )
    def _compute_partner_count(self):
        for wizard in self:
            if wizard._get_recipient_mode() == "domain":
                wizard.partner_count = self.env["res.partner"].search_count(
                    wizard._get_partner_domain()
                )
            else:
                partners = wizard.partner_ids
                if wizard.target == "existing":
                    partners |= wizard.mailing_list_id.partner_ids
                wizard.partner_count = len(partners)

    def _get_recipient_mode(self):
        """Return the mode the wizard will really apply.

        An existing list keeps its own mode whatever the wizard field says,
        which matters for callers that skip the onchange keeping the two in
        sync in the form.
        """
        self.ensure_one()
        if self.target == "existing" and self.mailing_list_id:
            return self.mailing_list_id.recipient_mode
        return self.recipient_mode

    @api.model
    def _domain_to_char(self, domain):
        """Serialise ``domain``, preferring ``[]`` over the ``TRUE`` leaf.

        ``Domain.OR`` collapses to ``[(1, '=', 1)]`` as soon as one of its
        operands matches everything, which is correct but reads as noise in
        the domain widget.
        """
        return "[]" if domain.is_true() else repr(list(domain))

    def _parse_domain(self, domain_string):
        """Return ``domain_string`` as a domain, matching nothing if unparsable."""
        try:
            return Domain(literal_eval(domain_string or "[]"))
        except (SyntaxError, TypeError, ValueError):
            _logger.warning(
                "Invalid domain on Liana mailing list wizard: %r", domain_string,
            )
            return Domain.FALSE

    def _get_view_domain(self):
        self.ensure_one()
        return self._parse_domain(self.view_domain)

    def _get_partner_domain(self):
        self.ensure_one()
        return self._parse_domain(self.partner_domain)

    def _apply(self):
        """Create or extend the mailing list and return it."""
        self.ensure_one()
        if self.target == "new":
            if not self.name:
                raise UserError(_("Please give the new mailing list a name."))
            values = {
                "name": self.name,
                "recipient_mode": self.recipient_mode,
                "liana_backend_id": self.liana_backend_id.id,
            }
            if self.recipient_mode == "domain":
                values["partner_domain"] = self.partner_domain
            else:
                values["partner_ids"] = [Command.set(self.partner_ids.ids)]
            return self.env["liana.mailing.list"].create(values)

        mailing_list = self.mailing_list_id
        if not mailing_list:
            raise UserError(_("Please select a mailing list."))
        if mailing_list.recipient_mode == "domain":
            mailing_list.partner_domain = self.partner_domain
        else:
            mailing_list.partner_ids = [
                Command.link(partner_id) for partner_id in self.partner_ids.ids
            ]
        return mailing_list

    def action_apply(self):
        mailing_list = self._apply()
        return self._notify(mailing_list, self._open_mailing_list(mailing_list))

    def action_apply_and_export(self):
        mailing_list = self._apply()
        mailing_list.action_export_to_liana()
        return self._notify(mailing_list, self._open_mailing_list(mailing_list))

    def _open_mailing_list(self, mailing_list):
        action = self.env["ir.actions.actions"]._for_xml_id(
            "connector_liana.action_liana_mailing_list"
        )
        action.update({
            "res_id": mailing_list.id,
            "views": [(False, "form")],
            "target": "current",
        })
        return action

    def _notify(self, mailing_list, next_action):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Liana Mailing List"),
                "message": _(
                    "%(list_name)s now targets %(count)s contact(s).",
                    list_name=mailing_list.name,
                    count=mailing_list.partner_count,
                ),
                "type": "success",
                "sticky": False,
                "next": next_action,
            },
        }
