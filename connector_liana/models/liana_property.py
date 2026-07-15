import logging

from odoo import api, fields, models

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
    )

    @api.model
    def _update_from_api(self, backend, items):
        """Create/update properties for the given backend from a property list.

        Stale properties for the backend that are not in the response are
        removed. Returns the recordset of properties currently present for the
        backend.
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

        stale = self.search([
            ("backend_id", "=", backend.id),
            ("handle", "not in", seen_handles),
        ])
        if stale:
            stale.unlink()

        return self.search([("backend_id", "=", backend.id)])
