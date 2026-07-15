from odoo import fields, models


class LianaFieldMapping(models.Model):
    _name = "liana.field.mapping"
    _description = "Liana Mailer Field Mapping"
    _order = "backend_id, id"

    backend_id = fields.Many2one(
        comodel_name="liana.backend",
        required=True,
        ondelete="cascade",
        index=True,
    )
    contact_field_id = fields.Many2one(
        comodel_name="ir.model.fields",
        string="Odoo Contact Field",
        required=True,
        ondelete="cascade",
        domain="[('model', '=', 'mailing.contact')]",
        help="Field on the mailing contact whose value is exported.",
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

    _sql_constraints = [
        (
            "mapping_field_unique",
            "UNIQUE(backend_id, contact_field_id)",
            "A contact field can only be mapped once per backend.",
        ),
    ]
