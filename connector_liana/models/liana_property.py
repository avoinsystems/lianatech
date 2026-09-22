import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .liana_backend import INTEGRATION_TYPE_MAILER

_logger = logging.getLogger(__name__)


class LianaProperty(models.Model):
    _name = "liana.property"
    _description = "Liana Mailer Property"
    _order = "name"

    name = fields.Char(
        required=True,
        help="Property name from the Liana Mailer API.",
    )
    handle = fields.Char(
        required=True,
        help="Property handle from the Liana Mailer API.",
    )
    property_type = fields.Char(string="Type")
    backend_id = fields.Many2one(
        comodel_name="liana.backend",
        required=True,
        ondelete="cascade",
        domain=[("integration_type", "=", INTEGRATION_TYPE_MAILER)],
    )

    @api.ondelete(at_uninstall=False)
    def _unlink_except_mapped(self):
        """Refuse to delete a property a field mapping still exports to."""
        mapped = self._get_mapped_properties()
        if mapped:
            raise UserError(_(
                "Liana propert(y/ies) %(properties)s cannot be deleted because "
                "the field mappings of their backend still export to them.",
                properties=", ".join(mapped.mapped("name")),
            ))

    def _get_mapped_properties(self):
        """Return the properties of this recordset used by a field mapping."""
        mappings = self.env["liana.field.mapping"].sudo().search([
            ("liana_property_id", "in", self.ids),
        ])
        return mappings.liana_property_id & self

    @api.model
    def _update_from_api(self, backend, items):
        """Create/update properties for the given backend from a property list.

        Stale properties for the backend that are not in the response are
        removed, unless a field mapping still exports to them: losing a
        property silently takes the mapping with it, and the next export would
        then ship a CSV without the corresponding column. Returns the
        recordset of properties currently present for the backend.
        """
        if not isinstance(items, list):
            _logger.warning(
                "Liana getCustomerProperties returned unexpected payload: %r", items
            )
            return self.browse()

        seen_handles = []
        for item in items:
            if not isinstance(item, dict):
                continue
            handle = item.get("handle")
            if not handle:
                continue
            seen_handles.append(handle)
            vals = {
                "name": item.get("name") or handle,
                "property_type": item.get("type") or False,
            }
            existing = self.search(
                [("backend_id", "=", backend.id), ("handle", "=", handle)],
                limit=1,
            )
            if existing:
                existing.write(vals)
            else:
                self.create({
                    **vals,
                    "handle": handle,
                    "backend_id": backend.id,
                })

        if not seen_handles:
            # A response without a single usable property is treated as a
            # glitch rather than as "this account has no properties": removing
            # them all would take the field mappings with them.
            _logger.warning(
                "Liana getCustomerProperties returned no property for backend "
                "%s (id=%s); keeping the known properties: %r",
                backend.name, backend.id, items,
            )
            return self.search([("backend_id", "=", backend.id)])

        stale = self.search([
            ("backend_id", "=", backend.id),
            ("handle", "not in", seen_handles),
        ])
        mapped = stale._get_mapped_properties()
        if mapped:
            _logger.warning(
                "Liana propert(y/ies) %s of backend %s (id=%s) are gone from "
                "Liana Mailer but are kept because field mappings export to them.",
                ", ".join(mapped.mapped("name")), backend.name, backend.id,
            )
        if stale - mapped:
            (stale - mapped).unlink()

        return self.search([("backend_id", "=", backend.id)])
