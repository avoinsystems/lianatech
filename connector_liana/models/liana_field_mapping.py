from odoo import fields, models

from .liana_backend import INTEGRATION_TYPE_MAILER


class LianaFieldMapping(models.Model):
    _name = "liana.field.mapping"
    _description = "Liana Mailer Field Mapping"
    _order = "backend_id, id"

    backend_id = fields.Many2one(
        comodel_name="liana.backend",
        required=True,
        ondelete="cascade",
        index=True,
        domain=[("integration_type", "=", INTEGRATION_TYPE_MAILER)],
    )
    partner_field_id = fields.Many2one(
        comodel_name="ir.model.fields",
        string="Odoo Partner Field",
        required=True,
        ondelete="cascade",
        domain="[('model', '=', 'res.partner')]",
        help="Field on the contact whose value is exported.",
    )
    liana_property_id = fields.Many2one(
        comodel_name="liana.property",
        string="Liana Property",
        required=True,
        ondelete="cascade",
        domain="[('backend_id', '=', backend_id)]",
    )
    liana_property_handle = fields.Char(
        related="liana_property_id.handle",
        string="Handle",
        readonly=True,
    )

    _mapping_field_unique = models.Constraint(
        "UNIQUE(backend_id, partner_field_id)",
        "A partner field can only be mapped once per backend.",
    )
