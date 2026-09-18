from odoo import Command, _, api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    liana_extra1 = fields.Char(
        string="Liana ID",
        help="External unique identifier sent to Liana Automation as 'extra1'.",
        readonly=True,
    )

    liana_mailer_event_ids = fields.One2many(
        comodel_name="liana.mailer.event",
        inverse_name="partner_id",
        string="Liana Mailer Events",
        groups="connector_liana.group_liana_mailing",
    )

    liana_mailer_event_count = fields.Integer(
        string="Liana Mailer Events Count",
        compute="_compute_liana_mailer_event_count",
        groups="connector_liana.group_liana_mailing",
    )

    liana_mailing_list_ids = fields.Many2many(
        comodel_name="liana.mailing.list",
        relation="liana_mailing_list_res_partner_rel",
        column1="partner_id",
        column2="list_id",
        string="Liana Mailing Lists",
        groups="connector_liana.group_liana_mailing",
        help="Liana mailing lists this contact was picked for one by one. "
             "Lists built from a search domain are not stored here.",
    )

    liana_mailing_list_count = fields.Integer(
        string="Liana Mailing Lists Count",
        compute="_compute_liana_mailing_list_count",
        groups="connector_liana.group_liana_mailing",
    )

    @api.depends("liana_mailer_event_ids")
    def _compute_liana_mailer_event_count(self):
        counts = dict(self.env["liana.mailer.event"]._read_group(
            domain=[("partner_id", "in", self.ids)],
            groupby=["partner_id"],
            aggregates=["__count"],
        ))
        for partner in self:
            partner.liana_mailer_event_count = counts.get(partner, 0)

    # Membership in a mailing list built from a search domain depends on the
    # whole contact, so there is nothing more specific to depend on than the
    # stored recipients of the manual lists.
    @api.depends("liana_mailing_list_ids")
    def _compute_liana_mailing_list_count(self):
        mailing_list_ids = self._liana_get_mailing_list_ids()
        for partner in self:
            partner.liana_mailing_list_count = len(mailing_list_ids.get(partner.id, ()))

    def _liana_get_mailing_list_ids(self):
        """Map each contact of this recordset to the ids of its mailing lists."""
        # A contact being created has no id to match a domain against yet.
        partners = self.filtered("id")
        domain_lists = self.env["liana.mailing.list"].search([
            ("recipient_mode", "=", "domain"),
        ])
        mailing_list_ids = domain_lists._match_partners(partners)
        for partner in partners:
            mailing_list_ids[partner.id].update(partner.liana_mailing_list_ids.ids)
        return mailing_list_ids

    def action_view_liana_mailing_lists(self):
        """Open the Liana mailing lists targeting this contact."""
        self.ensure_one()
        mailing_list_ids = sorted(self._liana_get_mailing_list_ids().get(self.id, ()))
        return {
            "type": "ir.actions.act_window",
            "name": _("Liana Mailing Lists"),
            "res_model": "liana.mailing.list",
            "view_mode": "list,form",
            "domain": [("id", "in", mailing_list_ids)],
            "context": {
                "default_partner_ids": [Command.set([self.id])],
            },
        }

    def action_view_liana_mailer_events(self):
        """Open the Liana Mailer events of this contact."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Liana Mailer Events"),
            "res_model": "liana.mailer.event",
            "view_mode": "list,form",
            "domain": [("partner_id", "=", self.id)],
            "context": {
                "default_partner_id": self.id,
                "search_default_group_by_event_type": 1,
            },
        }

    def _liana_ensure_extra1(self):
        """Give every contact of the recordset a Liana ID, keeping existing ones."""
        for partner in self.sudo().filtered(lambda rec: not rec.liana_extra1):
            partner.liana_extra1 = self.env["ir.sequence"].next_by_code("liana.extra1")
        return self
