from odoo import fields, models


class CrmLead(models.Model):
    _inherit = "crm.lead"

    liana_extra1 = fields.Char(
        string="Liana ID",
        help="External unique identifier sent to Liana Automation as 'extra1'.",
        readonly=True,
    )

    def _liana_ensure_extra1(self):
        """Give every lead of the recordset a Liana ID, keeping existing ones."""
        for lead in self.sudo().filtered(lambda rec: not rec.liana_extra1):
            lead.liana_extra1 = self.env["ir.sequence"].next_by_code("liana.extra1")
        return self
