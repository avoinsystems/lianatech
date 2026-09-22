from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestLianaPropertySync(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = cls.env["liana.backend"].create({
            "name": "Mailer backend",
            "integration_type": "mailer",
            "liana_mailer_address": "https://rest.example.test",
            "liana_mailer_secret": "mailer-secret",
            "liana_mailer_user": "mailer-user",
            "liana_mailer_realm": "MailerRealm",
        })
        cls.properties = cls.env["liana.property"]

    def _sync(self, items):
        return self.properties._update_from_api(self.backend, items)

    def _map_to_partner_field(self, liana_property, field_name="function"):
        field = self.env["ir.model.fields"]._get("res.partner", field_name)
        return self.env["liana.field.mapping"].create({
            "backend_id": self.backend.id,
            "partner_field_id": field.id,
            "liana_property_id": liana_property.id,
        })

    def test_properties_are_created_from_the_api(self):
        synced = self._sync([
            {"handle": "1", "name": "firstname", "type": "text"},
            {"handle": "2", "name": "company", "type": "text"},
        ])

        self.assertEqual(synced.mapped("name"), ["company", "firstname"])
        self.assertEqual(synced.mapped("handle"), ["2", "1"])
        self.assertEqual(set(synced.mapped("property_type")), {"text"})

    def test_a_renamed_property_is_updated_in_place(self):
        self._sync([{"handle": "1", "name": "firstname", "type": "text"}])
        synced = self._sync([{"handle": "1", "name": "given_name", "type": "string"}])

        self.assertEqual(len(synced), 1)
        self.assertEqual(synced.name, "given_name")
        self.assertEqual(synced.property_type, "string")

    def test_a_property_without_name_falls_back_to_its_handle(self):
        synced = self._sync([{"handle": "7"}])

        self.assertEqual(synced.name, "7")

    def test_a_removed_property_is_deleted(self):
        self._sync([
            {"handle": "1", "name": "firstname"},
            {"handle": "2", "name": "company"},
        ])
        synced = self._sync([{"handle": "1", "name": "firstname"}])

        self.assertEqual(synced.mapped("name"), ["firstname"])

    def test_a_removed_property_used_by_a_mapping_is_kept(self):
        self._sync([
            {"handle": "1", "name": "firstname"},
            {"handle": "2", "name": "company"},
        ])
        company = self.properties.search([
            ("backend_id", "=", self.backend.id), ("handle", "=", "2"),
        ])
        mapping = self._map_to_partner_field(company)

        synced = self._sync([{"handle": "1", "name": "firstname"}])

        self.assertEqual(synced.mapped("name"), ["company", "firstname"])
        self.assertTrue(mapping.exists())
        self.assertEqual(mapping.liana_property_id, company)

    def test_an_empty_response_keeps_the_known_properties(self):
        self._sync([{"handle": "1", "name": "firstname"}])

        synced = self._sync([])

        self.assertEqual(synced.mapped("name"), ["firstname"])

    def test_a_response_without_usable_property_keeps_the_known_ones(self):
        self._sync([{"handle": "1", "name": "firstname"}])

        synced = self._sync([{"name": "no handle here"}, "not a dict"])

        self.assertEqual(synced.mapped("name"), ["firstname"])

    def test_an_unexpected_payload_is_ignored(self):
        self._sync([{"handle": "1", "name": "firstname"}])

        self.assertFalse(self._sync({"result": "not a list"}))
        self.assertEqual(
            self.properties.search([("backend_id", "=", self.backend.id)]).mapped("name"),
            ["firstname"],
        )

    def test_a_mapped_property_cannot_be_deleted(self):
        synced = self._sync([{"handle": "1", "name": "firstname"}])
        self._map_to_partner_field(synced)

        with self.assertRaises(UserError) as cm:
            synced.unlink()
        self.assertIn("firstname", str(cm.exception))

    def test_an_unmapped_property_can_be_deleted(self):
        synced = self._sync([{"handle": "1", "name": "firstname"}])

        synced.unlink()

        self.assertFalse(synced.exists())

    def test_deleting_the_backend_removes_properties_and_mappings(self):
        synced = self._sync([{"handle": "1", "name": "firstname"}])
        mapping = self._map_to_partner_field(synced)

        self.backend.unlink()

        self.assertFalse(synced.exists())
        self.assertFalse(mapping.exists())
