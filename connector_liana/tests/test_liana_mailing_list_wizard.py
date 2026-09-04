from ast import literal_eval

from odoo.exceptions import AccessError
from odoo.tests import Form, tagged

from .test_liana_mailer import LianaMailerCommon


@tagged("post_install", "-at_install")
class TestLianaMailingListAdd(LianaMailerCommon):

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

    def _new_wizard(self, partners=None, domain=None, **values):
        partners = self.partners if partners is None else partners
        return self.env["liana.mailing.list.add"].with_context(
            active_model="res.partner",
            active_ids=partners.ids,
            active_domain=domain if domain is not None else [],
        ).create(values)

    def test_defaults_come_from_the_contacts_view(self):
        domain = [["ref", "=", "LIANA"]]
        wizard = self._new_wizard(partners=self.tagged_partners, domain=domain)

        self.assertEqual(wizard.partner_ids, self.tagged_partners)
        self.assertEqual(literal_eval(wizard.view_domain), domain)
        self.assertEqual(wizard.target, "new")
        self.assertEqual(wizard.recipient_mode, "manual")

    def test_new_manual_list_holds_the_selected_contacts(self):
        wizard = self._new_wizard(name="From selection")
        wizard.action_apply()

        mailing_list = self.env["liana.mailing.list"].search([
            ("name", "=", "From selection"),
        ])
        self.assertEqual(mailing_list.recipient_mode, "manual")
        self.assertEqual(mailing_list.partner_ids, self.partners)

    def test_new_domain_list_takes_the_view_domain(self):
        wizard = self._new_wizard(
            domain=[["ref", "=", "LIANA"]],
            name="From domain",
            recipient_mode="domain",
        )
        self.assertEqual(
            literal_eval(wizard.partner_domain), [("ref", "=", "LIANA")]
        )
        wizard.action_apply()

        mailing_list = self.env["liana.mailing.list"].search([
            ("name", "=", "From domain"),
        ])
        self.assertEqual(mailing_list.recipient_mode, "domain")
        self.assertEqual(mailing_list._get_recipients(), self.tagged_partners)

    def test_existing_manual_list_keeps_its_contacts(self):
        wizard = self._new_wizard(
            partners=self.tagged_partners,
            target="existing",
            mailing_list_id=self.mailing_list.id,
        )
        wizard.action_apply()

        self.assertEqual(
            self.mailing_list.partner_ids,
            self.partners + self.tagged_partners,
        )

    def test_existing_manual_list_ignores_a_repeated_contact(self):
        wizard = self._new_wizard(
            partners=self.partners,
            target="existing",
            mailing_list_id=self.mailing_list.id,
        )
        wizard.action_apply()

        self.assertEqual(self.mailing_list.partner_ids, self.partners)

    def test_existing_domain_list_combines_both_domains(self):
        wizard = self._new_wizard(
            domain=[["email", "=", "alice@example.test"]],
            target="existing",
            mailing_list_id=self.domain_list.id,
        )
        self.assertEqual(
            literal_eval(wizard.partner_domain),
            ["|", ("email", "=", "alice@example.test"), ("ref", "=", "LIANA")],
        )
        wizard.action_apply()

        self.assertEqual(
            self.domain_list._get_recipients(),
            self.partners[0] + self.tagged_partners,
        )

    def test_combined_domain_matching_everything_reads_as_empty(self):
        self.domain_list.partner_domain = "[]"
        wizard = self._new_wizard(
            domain=[["ref", "=", "LIANA"]],
            target="existing",
            mailing_list_id=self.domain_list.id,
        )

        # Domain.OR yields [(1, '=', 1)] here, which is correct but unreadable.
        self.assertEqual(wizard.partner_domain, "[]")

    def test_user_can_edit_the_combined_domain(self):
        wizard = self._new_wizard(
            domain=[["email", "=", "alice@example.test"]],
            target="existing",
            mailing_list_id=self.domain_list.id,
        )
        wizard.partner_domain = "[('email', '=', 'bob@example.test')]"
        wizard.action_apply()

        self.assertEqual(self.domain_list._get_recipients(), self.partners[1])

    def test_existing_list_mode_follows_the_list(self):
        form = Form(self.env["liana.mailing.list.add"].with_context(
            active_model="res.partner",
            active_ids=self.partners.ids,
            active_domain=[],
        ))
        form.target = "existing"
        form.mailing_list_id = self.domain_list

        self.assertEqual(form.recipient_mode, "domain")

    def test_existing_list_mode_cannot_be_switched(self):
        wizard = self._new_wizard(
            target="existing", mailing_list_id=self.domain_list.id,
        )
        wizard.recipient_mode = "manual"
        wizard.action_apply()

        self.assertEqual(self.domain_list.recipient_mode, "domain")
        self.assertFalse(self.domain_list.partner_ids)

    def test_recipient_count_previews_the_saved_list(self):
        wizard = self._new_wizard(
            partners=self.tagged_partners,
            target="existing",
            mailing_list_id=self.mailing_list.id,
        )
        self.assertEqual(wizard.partner_count, 4)

    def test_apply_and_export_pushes_the_list_to_liana(self):
        wizard = self._new_wizard(name="Exported right away")
        with self._patch_mailer(create_result=88) as mock_req:
            wizard.action_apply_and_export()

        mailing_list = self.env["liana.mailing.list"].search([
            ("name", "=", "Exported right away"),
        ])
        self.assertEqual(mailing_list.liana_list_id, 88)
        self.assertEqual(
            sorted(r["email"] for r in self._imported_recipients(mock_req)),
            ["alice@example.test", "bob@example.test"],
        )

    def test_action_is_bound_to_the_contacts_list(self):
        action = self.env.ref("connector_liana.action_liana_mailing_list_add")
        self.assertEqual(action.binding_model_id.model, "res.partner")
        self.assertEqual(action.binding_view_types, "list")
        self.assertIn(
            self.env.ref("connector_liana.group_liana_mailing"), action.group_ids,
        )


@tagged("post_install", "-at_install")
class TestLianaMailingListAddAccess(LianaMailerCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mailing_user = cls.env["res.users"].create({
            "name": "Liana Marketer",
            "login": "liana.marketer",
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("connector_liana.group_liana_mailing").id,
            ])],
        })
        cls.plain_user = cls.env["res.users"].create({
            "name": "Plain User",
            "login": "plain.user",
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
        })

    def test_mailing_group_can_create_a_list(self):
        wizard = self.env["liana.mailing.list.add"].with_user(
            self.mailing_user
        ).with_context(
            active_model="res.partner",
            active_ids=self.partners.ids,
            active_domain=[],
        ).create({"name": "Marketer list"})
        wizard.action_apply()

        mailing_list = self.env["liana.mailing.list"].search([
            ("name", "=", "Marketer list"),
        ])
        self.assertEqual(mailing_list.partner_ids, self.partners)

    def test_plain_user_cannot_use_the_wizard(self):
        with self.assertRaises(AccessError):
            self.env["liana.mailing.list.add"].with_user(
                self.plain_user
            ).create({"name": "Nope"})
