from odoo import Command
from odoo.tests import tagged

from .test_liana_mailer import LianaMailerCommon


@tagged("post_install", "-at_install")
class TestLianaPartnerMailingLists(LianaMailerCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.picked, cls.matching, cls.both, cls.outsider = cls.env["res.partner"].create([
            {"name": "Picked", "email": "picked@example.test"},
            {"name": "Matching", "email": "matching@example.test", "ref": "LIANA"},
            {"name": "Both", "email": "both@example.test", "ref": "LIANA"},
            {"name": "Outsider", "email": "outsider@example.test"},
        ])
        cls.manual_list = cls.env["liana.mailing.list"].create({
            "name": "Manual list",
            "liana_backend_id": cls.backend.id,
            "recipient_mode": "manual",
            "partner_ids": [Command.set((cls.picked + cls.both).ids)],
        })
        cls.domain_list = cls.env["liana.mailing.list"].create({
            "name": "Domain list",
            "liana_backend_id": cls.backend.id,
            "recipient_mode": "domain",
            "partner_domain": "[('ref', '=', 'LIANA')]",
        })

    def _count_queries(self, partner):
        """Return the number of queries spent on the count of ``partner``."""
        self.env.flush_all()
        self.env.invalidate_all()
        before = self.env.cr.sql_log_count
        partner.liana_mailing_list_count  # noqa: B018
        return self.env.cr.sql_log_count - before

    def test_a_picked_contact_is_counted(self):
        self.assertEqual(self.picked.liana_mailing_list_count, 1)
        self.assertEqual(
            self.picked._liana_get_mailing_list_ids()[self.picked.id],
            {self.manual_list.id},
        )

    def test_a_contact_matching_a_domain_is_counted(self):
        self.assertEqual(self.matching.liana_mailing_list_count, 1)
        self.assertEqual(
            self.matching._liana_get_mailing_list_ids()[self.matching.id],
            {self.domain_list.id},
        )

    def test_both_recipient_modes_add_up(self):
        self.assertEqual(self.both.liana_mailing_list_count, 2)
        self.assertEqual(
            self.both._liana_get_mailing_list_ids()[self.both.id],
            {self.manual_list.id, self.domain_list.id},
        )

    def test_a_contact_of_no_list_is_not_counted(self):
        self.assertEqual(self.outsider.liana_mailing_list_count, 0)

    def test_the_counts_of_a_recordset_come_from_one_batch(self):
        partners = self.picked + self.matching + self.both + self.outsider
        self.assertEqual(
            partners.mapped("liana_mailing_list_count"), [1, 1, 2, 0],
        )

    def test_a_new_contact_is_not_counted(self):
        partner = self.env["res.partner"].new({"name": "Unsaved"})
        self.assertEqual(partner.liana_mailing_list_count, 0)

    def test_an_unparsable_domain_is_ignored(self):
        # The constraint rejects such a domain, so it can only come from data
        # written before the list was given a domain the model can read.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE liana_mailing_list SET partner_domain = %s WHERE id = %s",
            ("[('ref', '=',", self.domain_list.id),
        )
        self.env.invalidate_all()

        self.assertEqual(self.matching.liana_mailing_list_count, 0)
        self.assertEqual(self.both.liana_mailing_list_count, 1)

    def test_the_action_opens_the_lists_of_the_contact(self):
        action = self.both.action_view_liana_mailing_lists()

        self.assertEqual(action["res_model"], "liana.mailing.list")
        self.assertEqual(
            action["domain"],
            [("id", "in", sorted([self.manual_list.id, self.domain_list.id]))],
        )
        self.assertEqual(
            self.env["liana.mailing.list"].search(action["domain"]),
            self.manual_list + self.domain_list,
        )

    def test_more_domain_lists_cost_no_more_queries(self):
        baseline = self._count_queries(self.matching)

        self.env["liana.mailing.list"].create([
            {
                "name": f"Domain list {index}",
                "liana_backend_id": self.backend.id,
                "recipient_mode": "domain",
                "partner_domain": f"[('ref', '=', 'LIANA-{index}')]",
            }
            for index in range(20)
        ])

        self.assertEqual(self._count_queries(self.matching), baseline)
