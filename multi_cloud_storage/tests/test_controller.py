# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from multi_cloud_storage import controller
from multi_cloud_storage.backends.gcs_backend import GCSBackend
from multi_cloud_storage.backends.s3_backend import S3Backend


def _config(provider):
	return frappe._dict(storage_provider=provider, enabled=1)


class TestGetBackend(IntegrationTestCase):
	"""
	Regression coverage for get_backend()'s per-provider lazy import.

	Historically controller.py imported S3Backend, GCSBackend and AzureBackend
	at module level. A missing optional SDK for *any one* provider (e.g. the
	azure-storage-blob / azure-identity packages not being installed) raised
	ModuleNotFoundError while importing controller.py itself, which broke
	every whitelisted method in the module -- including test_connection,
	file_upload_to_cloud and delete_from_cloud for providers that don't even
	need the missing package (S3, GCS). These tests lock in the fix: each
	provider's import is isolated, so one missing SDK can only ever affect
	its own provider.
	"""

	def test_returns_none_when_no_config(self):
		with patch("multi_cloud_storage.controller.get_config", return_value=None):
			self.assertIsNone(controller.get_backend(None))

	def test_returns_none_for_unknown_provider(self):
		self.assertIsNone(controller.get_backend(_config("Not A Real Provider")))

	def test_dispatches_to_s3_backend(self):
		backend = controller.get_backend(_config("Amazon S3"))
		self.assertIsInstance(backend, S3Backend)

	def test_dispatches_to_gcs_backend(self):
		backend = controller.get_backend(_config("Google Cloud Storage"))
		self.assertIsInstance(backend, GCSBackend)

	def test_missing_backend_dependency_raises_actionable_error(self):
		with patch("multi_cloud_storage.controller.importlib") as mock_importlib:
			mock_importlib.import_module.side_effect = ModuleNotFoundError("No module named 'azure'")
			with self.assertRaises(frappe.ValidationError) as ctx:
				controller.get_backend(_config("Azure Blob Storage"))
		message = str(ctx.exception)
		self.assertIn("bench pip install", message)
		self.assertIn("azure-storage-blob", message)
		self.assertIn("azure-identity", message)

	def test_missing_azure_dependency_does_not_break_other_providers(self):
		"""The exact regression from production: azure missing must not affect S3/GCS."""
		import importlib as real_importlib

		def fake_import_module(name, package=None):
			if "azure" in name:
				raise ModuleNotFoundError("No module named 'azure'")
			return real_importlib.import_module(name, package=package)

		with patch("multi_cloud_storage.controller.importlib") as mock_importlib:
			mock_importlib.import_module.side_effect = fake_import_module

			s3_backend = controller.get_backend(_config("Amazon S3"))
			self.assertIsInstance(s3_backend, S3Backend)

			gcs_backend = controller.get_backend(_config("Google Cloud Storage"))
			self.assertIsInstance(gcs_backend, GCSBackend)

			with self.assertRaises(frappe.ValidationError):
				controller.get_backend(_config("Azure Blob Storage"))
