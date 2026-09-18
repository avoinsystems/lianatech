from unittest.mock import patch

import requests

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.connector_liana.models import liana_backend as liana_backend_module


class TestLianaBackendAutomationConnection(TransactionCase):

    def _create_full_backend(self):
        return self.env["liana.backend"].create(
            {
                "name": "Test backend",
                "liana_automation_address": "https://automation.example.test",
                "liana_automation_secret": "test-secret-key",
                "liana_automation_user": "api-user",
                "liana_automation_realm": "TestRealm",
            }
        )

    @patch.object(liana_backend_module.requests, "post")
    def test_test_automation_connection_success(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.json.return_value = {"pong": "pong"}
        mock_response.raise_for_status.return_value = None

        backend = self._create_full_backend()
        result = backend.test_automation_connection()

        mock_post.assert_called_once()
        called_kwargs = mock_post.call_args.kwargs
        self.assertEqual(called_kwargs.get("data"), '{"ping": "pong"}')
        self.assertIn("Authorization", called_kwargs["headers"])
        self.assertEqual(
            called_kwargs["timeout"], liana_backend_module.REQUEST_TIMEOUT,
        )

        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(result["params"]["type"], "success")

    def test_test_automation_connection_missing_settings(self):
        backend = self.env["liana.backend"].create(
            {
                "name": "Incomplete",
                "liana_automation_address": "https://example.test",
            }
        )
        with self.assertRaises(UserError) as cm:
            backend.test_automation_connection()
        self.assertIn("Please fill in all Liana Automation settings", str(cm.exception))

    @patch.object(liana_backend_module.requests, "post")
    def test_test_automation_connection_unexpected_response(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.json.return_value = {"unexpected": True}
        mock_response.raise_for_status.return_value = None

        backend = self._create_full_backend()
        with self.assertRaises(UserError) as cm:
            backend.test_automation_connection()
        self.assertIn("Unexpected response from Liana Automation", str(cm.exception))

    @patch.object(liana_backend_module.requests, "post")
    def test_test_automation_connection_request_failure(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("too slow")

        backend = self._create_full_backend()
        with self.assertRaises(UserError) as cm:
            backend.test_automation_connection()
        self.assertIn("API request failed", str(cm.exception))


class TestLianaBackendMailerConnection(TransactionCase):

    def _create_full_backend(self):
        return self.env["liana.backend"].create(
            {
                "name": "Test mailer backend",
                "integration_type": "mailer",
                "liana_mailer_address": "https://rest.example.test",
                "liana_mailer_secret": "mailer-secret",
                "liana_mailer_user": "mailer-user",
                "liana_mailer_realm": "MailerRealm",
            }
        )

    @patch.object(liana_backend_module.requests, "post")
    def test_test_mailer_connection_success(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.json.return_value = {"result": "hello"}
        mock_response.raise_for_status.return_value = None

        backend = self._create_full_backend()
        result = backend.test_mailer_connection()

        mock_post.assert_called_once()
        called_kwargs = mock_post.call_args.kwargs
        self.assertEqual(called_kwargs.get("data"), '["hello"]')
        self.assertEqual(
            called_kwargs["timeout"], liana_backend_module.REQUEST_TIMEOUT,
        )
        self.assertEqual(result["params"]["type"], "success")

    def test_test_mailer_connection_missing_settings(self):
        backend = self.env["liana.backend"].create(
            {
                "name": "Incomplete",
                "integration_type": "mailer",
                "liana_mailer_address": "https://rest.example.test",
            }
        )
        with self.assertRaises(UserError) as cm:
            backend.test_mailer_connection()
        self.assertIn("Please fill in all Liana Mailer settings", str(cm.exception))

    @patch.object(liana_backend_module.requests, "post")
    def test_test_mailer_connection_unexpected_response(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.json.return_value = {"result": "goodbye"}
        mock_response.raise_for_status.return_value = None

        backend = self._create_full_backend()
        with self.assertRaises(UserError) as cm:
            backend.test_mailer_connection()
        self.assertIn("Unexpected response from Liana Mailer", str(cm.exception))

    @patch.object(liana_backend_module.requests, "post")
    def test_test_mailer_connection_request_failure(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("too slow")

        backend = self._create_full_backend()
        with self.assertRaises(UserError) as cm:
            backend.test_mailer_connection()
        self.assertIn("API request failed", str(cm.exception))


@tagged("post_install", "-at_install")
class TestLianaBackendIntegrationType(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.automation_backend = cls.env["liana.backend"].create({
            "name": "Automation backend",
            "integration_type": "automation",
        })
        cls.mailer_backend = cls.env["liana.backend"].create({
            "name": "Mailer backend",
            "integration_type": "mailer",
        })

    def test_default_integration_type_is_automation(self):
        backend = self.env["liana.backend"].create({"name": "Untyped"})
        self.assertEqual(backend.integration_type, "automation")

    def test_default_backend_is_scoped_by_integration_type(self):
        backends = self.env["liana.backend"]
        self.assertEqual(
            backends._get_default_backend("automation"), self.automation_backend
        )
        self.assertEqual(
            backends._get_default_backend("mailer"), self.mailer_backend
        )
        # Untyped, both backends match, so there is no unique one to pick.
        self.assertFalse(backends._get_default_backend())

    def test_mailer_backend_rejected_on_liana_action(self):
        partner_model = self.env.ref("base.model_res_partner")
        automation = self.env["base.automation"].create({
            "name": "Liana export on partner write",
            "model_id": partner_model.id,
            "trigger": "on_create_or_write",
            "trigger_field_ids": [(6, 0, [
                self.env["ir.model.fields"]._get("res.partner", "name").id,
            ])],
        })
        with self.assertRaises(ValidationError) as cm:
            self.env["ir.actions.server"].create({
                "name": "Send to Liana",
                "model_id": partner_model.id,
                "state": "liana",
                "usage": "base_automation",
                "base_automation_id": automation.id,
                "liana_backend_id": self.mailer_backend.id,
                "liana_event_verb": "subscribe",
            })
        self.assertIn("not a Liana Automation backend", str(cm.exception))

    def test_automation_backend_rejected_on_mailing_list(self):
        with self.assertRaises(ValidationError) as cm:
            self.env["liana.mailing.list"].create({
                "name": "Newsletter",
                "liana_backend_id": self.automation_backend.id,
            })
        self.assertIn("not a Liana Mailer backend", str(cm.exception))

    def test_mailer_backend_rejected_on_liana_event(self):
        with self.assertRaises(ValidationError) as cm:
            self.env["liana.event"].create({
                "name": "Manual event",
                "backend_id": self.mailer_backend.id,
            })
        self.assertIn("not a Liana Automation backend", str(cm.exception))

    def test_unused_backend_can_be_retyped(self):
        self.automation_backend.integration_type = "mailer"
        self.assertEqual(self.automation_backend.integration_type, "mailer")

    def test_retyping_referenced_backend_raises(self):
        self.env["liana.channel"].create({
            "name": "Test channel",
            "channel_id": 1,
            "backend_id": self.automation_backend.id,
        })
        with self.assertRaises(UserError) as cm:
            self.automation_backend.integration_type = "mailer"
        self.assertIn("cannot change integration", str(cm.exception))

    def test_retyping_backend_referenced_by_mailing_list_raises(self):
        self.env["liana.mailing.list"].create({
            "name": "Newsletter",
            "liana_backend_id": self.mailer_backend.id,
        })
        with self.assertRaises(UserError) as cm:
            self.mailer_backend.integration_type = "automation"
        self.assertIn("cannot change integration", str(cm.exception))

    def test_mailer_calls_refused_on_automation_backend(self):
        self.automation_backend.write({
            "liana_mailer_address": "https://rest.example.test",
            "liana_mailer_secret": "secret",
            "liana_mailer_user": "user",
            "liana_mailer_realm": "Realm",
        })
        with self.assertRaises(UserError) as cm:
            self.automation_backend.test_mailer_connection()
        self.assertIn("cannot be used for Liana Mailer", str(cm.exception))

    def test_automation_calls_refused_on_mailer_backend(self):
        self.mailer_backend.write({
            "liana_automation_address": "https://automation.example.test",
            "liana_automation_secret": "secret",
            "liana_automation_user": "user",
            "liana_automation_realm": "Realm",
        })
        with self.assertRaises(UserError) as cm:
            self.mailer_backend.test_automation_connection()
        self.assertIn("cannot be used for Liana Automation", str(cm.exception))
