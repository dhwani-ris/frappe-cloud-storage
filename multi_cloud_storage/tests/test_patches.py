# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from multi_cloud_storage.patches.v1_0 import set_default_excluded_file_extensions as backfill_patch


class TestSetDefaultExcludedFileExtensions(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._original = frappe.db.get_single_value("Cloud Storage Configuration", "excluded_file_extensions")

	def tearDown(self):
		frappe.db.set_single_value("Cloud Storage Configuration", "excluded_file_extensions", self._original)
		self._commit_patch.stop()
		super().tearDown()

	def test_backfills_default_when_unset(self):
		frappe.db.set_single_value("Cloud Storage Configuration", "excluded_file_extensions", "")
		backfill_patch.execute()
		value = frappe.db.get_single_value("Cloud Storage Configuration", "excluded_file_extensions")
		self.assertEqual(value, backfill_patch.DEFAULT_EXCLUDED_EXTENSIONS)

	def test_does_not_overwrite_an_already_set_value(self):
		frappe.db.set_single_value("Cloud Storage Configuration", "excluded_file_extensions", ".custom")
		backfill_patch.execute()
		value = frappe.db.get_single_value("Cloud Storage Configuration", "excluded_file_extensions")
		self.assertEqual(value, ".custom")
