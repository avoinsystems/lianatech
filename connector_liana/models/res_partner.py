from odoo import _, api, fields, models


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
    )

    liana_mailer_event_count = fields.Integer(
        string="Liana Mailer Events Count",
        compute="_compute_liana_mailer_event_count",
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
