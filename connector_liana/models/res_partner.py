from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    liana_extra1 = fields.Char(
        string="Liana ID",
        help="External unique identifier sent to Liana Automation as 'extra1'.",
        readonly=True,
    )

