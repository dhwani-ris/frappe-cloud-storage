# Copyright (c) 2026, Bhushan Barbuddhe and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestCloudStorageAttachFieldTarget(IntegrationTestCase):
	"""
	Validates the guardrail that keeps this config list honest: every row must point at
	a real Attach / Attach Image field, since the reconciliation engine trusts this list
	blindly when deciding what to scan and update.
	"""

	def test_rejects_field_that_does_not_exist(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Cloud Storage Attach Field Target",
					"target_doctype": "File",
					"target_fieldname": "not_a_real_field",
					"bucket_type": "private",
				}
			).insert(ignore_permissions=True)

	def test_rejects_field_that_is_not_attach_type(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Cloud Storage Attach Field Target",
					"target_doctype": "File",
					"target_fieldname": "file_name",
					"bucket_type": "private",
				}
			).insert(ignore_permissions=True)

	def test_accepts_real_attach_field(self):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Attach Field Target",
				"target_doctype": "User",
				"target_fieldname": "user_image",
				"bucket_type": "private",
			}
		).insert(ignore_permissions=True)
		try:
			self.assertEqual(doc.name, "User-user_image")
		finally:
			frappe.delete_doc(
				"Cloud Storage Attach Field Target", doc.name, force=True, ignore_permissions=True
			)
