import json
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.connector_liana.models import liana_backend as liana_backend_module


class LianaMailerCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = cls.env["liana.backend"].create({
            "name": "Mailer backend",
            "liana_mailer_address": "https://rest.example.test",
            "liana_mailer_secret": "mailer-secret",
            "liana_mailer_user": "mailer-user",
            "liana_mailer_realm": "MailerRealm",
        })
        cls.partners = cls.env["res.partner"].create([
            {
                "name": "Alice",
                "email": "alice@example.test",
                "function": "ACME",
            },
            {
                "name": "Bob",
                "email": "bob@example.test",
                "function": "Globex",
            },
        ])
        cls.mailing_list = cls.env["liana.mailing.list"].create({
            "name": "Newsletter",
            "liana_backend_id": cls.backend.id,
            "partner_ids": [(6, 0, cls.partners.ids)],
        })

    def _make_side_effect(self, create_result=42, responses=None):
        """Return a callable emulating ``mailer_send_api_request`` by path.

        ``responses`` maps an endpoint path to the value it should return;
        endpoints not listed default to a successful envelope.
        """
        responses = responses or {}

        def _side_effect(path, params):
            if path in responses:
                return responses[path]
            if path == "v1/createMailingList":
                return {"result": create_result}
            return {"succeed": True}

        return _side_effect

    def _patch_mailer(self, **kwargs):
        return patch.object(
            liana_backend_module.LianaBackend,
            "mailer_send_api_request",
            side_effect=self._make_side_effect(**kwargs),
            autospec=False,
        )


@tagged("post_install", "-at_install")
class TestLianaMailerExport(LianaMailerCommon):

    def test_export_creates_list_and_imports(self):
        with self._patch_mailer(create_result=77) as mock_req:
            result = self.mailing_list.action_export_to_liana()

        self.assertEqual(self.mailing_list.liana_list_id, 77)
        self.assertTrue(self.mailing_list.date_liana_export)

        paths = [call.args[0] for call in mock_req.call_args_list]
        self.assertEqual(paths, ["v1/createMailingList", "v1/importJSONToMailingList"])

        create_params = mock_req.call_args_list[0].args[1]
        self.assertEqual(create_params, ["Newsletter", "Newsletter"])

        import_params = mock_req.call_args_list[1].args[1]
        self.assertEqual(import_params[0], 77)
        recipients = json.loads(import_params[1])
        self.assertEqual(
            sorted(r["email"] for r in recipients),
            ["alice@example.test", "bob@example.test"],
        )

        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["params"]["type"], "success")
        self.assertIn("2 recipient(s)", result["params"]["message"])

    def test_export_reuses_existing_list_id(self):
        self.mailing_list.liana_list_id = 500
        with self._patch_mailer() as mock_req:
            self.mailing_list.action_export_to_liana()

        paths = [call.args[0] for call in mock_req.call_args_list]
        self.assertEqual(paths, ["v1/importJSONToMailingList"])
        self.assertEqual(mock_req.call_args_list[0].args[1][0], 500)
        self.assertEqual(self.mailing_list.liana_list_id, 500)

    def test_export_truncates_when_enabled(self):
        self.mailing_list.write({"liana_list_id": 12, "liana_truncate": True})
        with self._patch_mailer() as mock_req:
            self.mailing_list.action_export_to_liana()

        paths = [call.args[0] for call in mock_req.call_args_list]
        self.assertEqual(
            paths, ["v1/truncateMailingList", "v1/importJSONToMailingList"]
        )
        self.assertEqual(mock_req.call_args_list[0].args[1], [12])

    def test_export_skips_contacts_without_email(self):
        no_email = self.env["res.partner"].create({"name": "No Email"})
        self.mailing_list.partner_ids = [(4, no_email.id)]
        with self._patch_mailer() as mock_req:
            self.mailing_list.action_export_to_liana()

        import_params = mock_req.call_args_list[-1].args[1]
        recipients = json.loads(import_params[1])
        self.assertEqual(len(recipients), 2)

    def test_export_includes_mapped_properties(self):
        prop = self.env["liana.property"].create({
            "name": "company",
            "handle": "company",
            "backend_id": self.backend.id,
        })
        function_field = self.env["ir.model.fields"]._get("res.partner", "function")
        self.env["liana.field.mapping"].create({
            "backend_id": self.backend.id,
            "partner_field_id": function_field.id,
            "liana_property_id": prop.id,
        })

        with self._patch_mailer() as mock_req:
            self.mailing_list.action_export_to_liana()

        import_params = mock_req.call_args_list[-1].args[1]
        recipients = json.loads(import_params[1])
        by_email = {r["email"]: r for r in recipients}
        self.assertEqual(by_email["alice@example.test"]["company"], "ACME")
        self.assertEqual(by_email["bob@example.test"]["company"], "Globex")

    def test_export_missing_mailer_settings(self):
        self.mailing_list.liana_backend_id = self.env["liana.backend"].create({
            "name": "Incomplete",
            "liana_mailer_address": "https://rest.example.test",
        })
        with self._patch_mailer() as mock_req:
            with self.assertRaises(UserError) as cm:
                self.mailing_list.action_export_to_liana()
        self.assertIn("Liana Mailer settings", str(cm.exception))
        mock_req.assert_not_called()

    def test_export_no_backend_raises(self):
        self.backend.unlink()
        self.mailing_list.liana_backend_id = False
        with self.assertRaises(UserError) as cm:
            self.mailing_list.action_export_to_liana()
        self.assertIn("No Liana backend", str(cm.exception))

    def test_export_uses_default_backend_when_unset(self):
        self.mailing_list.liana_backend_id = False
        with self._patch_mailer(create_result=9):
            self.mailing_list.action_export_to_liana()
        self.assertEqual(self.mailing_list.liana_list_id, 9)

    def test_export_create_list_unexpected_response(self):
        responses = {"v1/createMailingList": {"succeed": True}}
        with self._patch_mailer(responses=responses):
            with self.assertRaises(UserError) as cm:
                self.mailing_list.action_export_to_liana()
        self.assertIn("createMailingList", str(cm.exception))
        self.assertFalse(self.mailing_list.liana_list_id)
        self.assertFalse(self.mailing_list.date_liana_export)

    def test_export_import_failure_raises(self):
        responses = {
            "v1/importJSONToMailingList": {
                "succeed": False,
                "message": "quota exceeded",
            },
        }
        with self._patch_mailer(create_result=3, responses=responses):
            with self.assertRaises(UserError) as cm:
                self.mailing_list.action_export_to_liana()
        self.assertIn("quota exceeded", str(cm.exception))
        # A failed import leaves the list unmarked (no export timestamp).
        self.assertFalse(self.mailing_list.date_liana_export)

    def test_cron_exports_all_lists(self):
        other_list = self.env["liana.mailing.list"].create({
            "name": "Second list",
            "liana_backend_id": self.backend.id,
        })
        with self._patch_mailer(create_result=1):
            self.env["liana.mailing.list"]._cron_export_all_to_liana()

        self.assertTrue(self.mailing_list.date_liana_export)
        self.assertTrue(other_list.date_liana_export)

    def test_cron_swallows_user_errors(self):
        broken_list = self.env["liana.mailing.list"].create({
            "name": "Broken list",
            "liana_backend_id": self.env["liana.backend"].create({
                "name": "No mailer creds",
            }).id,
        })
        with self._patch_mailer(create_result=1):
            self.env["liana.mailing.list"]._cron_export_all_to_liana()

        # The healthy list still exported despite the broken one raising.
        self.assertTrue(self.mailing_list.date_liana_export)
        self.assertFalse(broken_list.date_liana_export)

    def test_partner_count(self):
        self.assertEqual(self.mailing_list.partner_count, 2)


@tagged("post_install", "-at_install")
class TestLianaMailingListRecipientMode(LianaMailerCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tagged_partners = cls.env["res.partner"].create([
            {"name": "Carol", "email": "carol@example.test", "ref": "LIANA"},
            {"name": "Dave", "email": "dave@example.test", "ref": "LIANA"},
        ])
        cls.domain_list = cls.env["liana.mailing.list"].create({
            "name": "Domain list",
            "liana_backend_id": cls.backend.id,
            "recipient_mode": "domain",
            "partner_domain": "[('ref', '=', 'LIANA')]",
        })

    def test_default_mode_is_manual(self):
        self.assertEqual(self.mailing_list.recipient_mode, "manual")

    def test_domain_mode_partner_count(self):
        self.assertEqual(self.domain_list.partner_count, 2)

    def test_domain_mode_count_follows_matching_contacts(self):
        self.env["res.partner"].create({
            "name": "Erin",
            "email": "erin@example.test",
            "ref": "LIANA",
        })
        self.domain_list.invalidate_recordset(["partner_count"])
        self.assertEqual(self.domain_list.partner_count, 3)

    def test_domain_mode_exports_matching_contacts(self):
        with self._patch_mailer(create_result=21) as mock_req:
            self.domain_list.action_export_to_liana()

        import_params = mock_req.call_args_list[-1].args[1]
        recipients = json.loads(import_params[1])
        self.assertEqual(
            sorted(r["email"] for r in recipients),
            ["carol@example.test", "dave@example.test"],
        )

    def test_domain_mode_ignores_selected_contacts(self):
        self.domain_list.partner_ids = [(6, 0, self.partners.ids)]
        with self._patch_mailer(create_result=22) as mock_req:
            self.domain_list.action_export_to_liana()

        import_params = mock_req.call_args_list[-1].args[1]
        recipients = json.loads(import_params[1])
        self.assertEqual(
            sorted(r["email"] for r in recipients),
            ["carol@example.test", "dave@example.test"],
        )

    def test_manual_mode_ignores_domain(self):
        self.mailing_list.partner_domain = "[('ref', '=', 'LIANA')]"
        with self._patch_mailer(create_result=23) as mock_req:
            self.mailing_list.action_export_to_liana()

        import_params = mock_req.call_args_list[-1].args[1]
        recipients = json.loads(import_params[1])
        self.assertEqual(
            sorted(r["email"] for r in recipients),
            ["alice@example.test", "bob@example.test"],
        )

    def test_domain_mode_skips_contacts_without_email(self):
        self.env["res.partner"].create({"name": "No Email", "ref": "LIANA"})
        with self._patch_mailer(create_result=24) as mock_req:
            self.domain_list.action_export_to_liana()

        import_params = mock_req.call_args_list[-1].args[1]
        recipients = json.loads(import_params[1])
        self.assertEqual(len(recipients), 2)

    def test_empty_domain_matches_all_contacts(self):
        self.domain_list.partner_domain = "[]"
        expected = self.env["res.partner"].search_count([])
        self.assertEqual(self.domain_list.partner_count, expected)

    def test_malformed_domain_rejected(self):
        with self.assertRaises(ValidationError):
            self.domain_list.partner_domain = "not a domain"

    def test_domain_on_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):
            self.domain_list.partner_domain = "[('no_such_field', '=', 1)]"

    def test_malformed_domain_allowed_in_manual_mode(self):
        self.mailing_list.partner_domain = "not a domain"
        self.assertEqual(self.mailing_list.partner_count, 2)
