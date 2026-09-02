# Copyright (c) 2026, Bhushan Barbuddhe and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestCloudStorageReconciliationIssue(IntegrationTestCase):
	"""
	The review queue only does its job if closing an issue actually records what the
	correct answer was -- these tests lock in the guardrail that stops a reviewer from
	marking an issue Resolved without ever recording a Resolved Key.
	"""

	def setUp(self):
		super().setUp()
		self._issues = []

	def tearDown(self):
		for name in self._issues:
			try:
				frappe.delete_doc(
					"Cloud Storage Reconciliation Issue", name, force=True, ignore_permissions=True
				)
			except Exception:
				pass
		super().tearDown()

	def _make_issue(self, **overrides):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Reconciliation Issue",
				"source_doctype": "User",
				"source_name": "Administrator",
				"raw_reference": "https://legacy.example.com/old/receipt.pdf",
				"reason": "Unresolved",
				"action_required": "Locate the correct object key by hand, then set Resolved Key.",
				**overrides,
			}
		).insert(ignore_permissions=True)
		self._issues.append(doc.name)
		return doc

	def test_cannot_mark_resolved_without_a_resolved_key(self):
		issue = self._make_issue()
		issue.status = "Resolved"
		with self.assertRaises(frappe.ValidationError):
			issue.save(ignore_permissions=True)

	def test_can_mark_resolved_once_resolved_key_is_set(self):
		issue = self._make_issue()
		issue.status = "Resolved"
		issue.resolved_key = "beam/2026/receipt-001.pdf"
		issue.save(ignore_permissions=True)
		issue.reload()
		self.assertEqual(issue.status, "Resolved")

	def test_staying_open_never_requires_a_resolved_key(self):
		issue = self._make_issue()
		issue.resolution_notes = "Still chasing this one down."
		issue.save(ignore_permissions=True)
		issue.reload()
		self.assertEqual(issue.status, "Open")

	def test_action_required_is_mandatory_at_creation(self):
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc(
				{
					"doctype": "Cloud Storage Reconciliation Issue",
					"source_doctype": "User",
					"source_name": "Administrator",
					"reason": "Unresolved",
				}
			).insert(ignore_permissions=True)
