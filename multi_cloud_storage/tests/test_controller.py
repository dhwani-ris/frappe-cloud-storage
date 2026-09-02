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


class _FakeBackend:
	"""In-memory stand-in used by the reconciliation-primitive tests below."""

	def __init__(self, existing_keys=None):
		self.existing_keys = set(existing_keys or [])
		self.exist_calls = []
		self.get_url_calls = []

	def object_exists(self, key, bucket_type="private"):
		self.exist_calls.append((key, bucket_type))
		return key in self.existing_keys

	def get_public_url(self, key):
		return f"https://fake-cloud.example.com/{key}"

	def get_url(self, key, file_name=None, bucket_type="private", as_attachment=False):
		self.get_url_calls.append((key, file_name, bucket_type, as_attachment))
		return f"https://fake-cloud.example.com/{key}?signed=1"


class TestComputeCloudReference(IntegrationTestCase):
	"""
	The core correction driving this whole feature: a private file's file_url must
	never be a raw signed URL (it expires in signed_url_expiry_time, default 300s) --
	it must always be the generate_file redirect that mints one on demand.
	"""

	def test_private_file_gets_generate_file_redirect_not_raw_url(self):
		backend = _FakeBackend()
		file_url, content_hash = controller._compute_cloud_reference(
			backend, "a/b.pdf", is_private=True, file_name="b.pdf"
		)
		self.assertEqual(content_hash, "private:a/b.pdf")
		self.assertTrue(file_url.startswith("/api/method/multi_cloud_storage.controller.generate_file?key="))
		self.assertIn("file_name=b.pdf", file_url)

	def test_public_file_gets_backend_public_url(self):
		backend = _FakeBackend()
		file_url, content_hash = controller._compute_cloud_reference(backend, "a/b.pdf", is_private=False)
		self.assertEqual(content_hash, "public:a/b.pdf")
		self.assertEqual(file_url, "https://fake-cloud.example.com/a/b.pdf")

	def test_public_file_without_get_public_url_falls_back(self):
		class NoPublicUrlBackend:
			pass

		file_url, _ = controller._compute_cloud_reference(
			NoPublicUrlBackend(), "a/b.pdf", is_private=False, public_fallback_url="/files/original.pdf"
		)
		self.assertEqual(file_url, "/files/original.pdf")


class TestLinkExistingObject(IntegrationTestCase):
	"""
	link_existing_object() is the primitive both the single-file admin endpoint and the
	bulk migration engine build on. The verify flag is the scale-critical bit: bulk
	callers (which already trust a freshly-scanned Cloud Storage Object Index) must be
	able to skip the live existence check entirely -- see the docstring in
	controller.py for why a per-row HEAD check doesn't scale to millions of rows.
	"""

	def setUp(self):
		super().setUp()
		self._file_docs = []

	def tearDown(self):
		for name in self._file_docs:
			try:
				frappe.delete_doc("File", name, force=True, ignore_permissions=True, delete_permanently=True)
			except Exception:
				pass
		super().tearDown()

	def _make_file(self, file_url):
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "x.pdf",
				"file_url": file_url,
				"is_private": 1,
				"content_hash": frappe.generate_hash(length=20),
			}
		).insert(ignore_permissions=True)
		self._file_docs.append(doc.name)
		return doc

	def test_no_backend_returns_no_backend(self):
		doc = self._make_file("https://legacy.example.com/a.pdf")
		result = controller.link_existing_object(
			doc, "a/x.pdf", "private", backend=None, config=frappe._dict(enabled=0)
		)
		self.assertEqual(result, "no_backend")

	def test_verify_true_checks_existence_and_reports_not_found(self):
		doc = self._make_file("https://legacy.example.com/a.pdf")
		backend = _FakeBackend(existing_keys=[])
		result = controller.link_existing_object(doc, "a/x.pdf", "private", backend=backend, verify=True)
		self.assertEqual(result, "not_found")
		self.assertEqual(backend.exist_calls, [("a/x.pdf", "private")])
		doc.reload()
		self.assertEqual(doc.file_url, "https://legacy.example.com/a.pdf")

	def test_verify_true_links_when_object_exists(self):
		doc = self._make_file("https://legacy.example.com/a.pdf")
		backend = _FakeBackend(existing_keys=["a/x.pdf"])
		result = controller.link_existing_object(doc, "a/x.pdf", "private", backend=backend, verify=True)
		self.assertEqual(result, "linked")
		doc.reload()
		self.assertEqual(doc.content_hash, "private:a/x.pdf")

	def test_verify_false_skips_existence_check_entirely(self):
		doc = self._make_file("https://legacy.example.com/a.pdf")
		backend = _FakeBackend(existing_keys=[])
		result = controller.link_existing_object(doc, "a/x.pdf", "private", backend=backend, verify=False)
		self.assertEqual(result, "linked")
		self.assertEqual(backend.exist_calls, [])
		doc.reload()
		self.assertEqual(doc.content_hash, "private:a/x.pdf")


class TestLookupByBasename(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self._index_rows = []

	def tearDown(self):
		for name in self._index_rows:
			try:
				frappe.delete_doc("Cloud Storage Object Index", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		super().tearDown()

	def _index(self, key, bucket_type="private"):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Object Index",
				"bucket_type": bucket_type,
				"object_key": key,
				"basename": key.rsplit("/", 1)[-1],
			}
		).insert(ignore_permissions=True)
		self._index_rows.append(doc.name)
		return doc

	def test_no_match_returns_empty_list(self):
		self.assertEqual(controller.lookup_by_basename("nothing-like-this.pdf"), [])

	def test_single_match_returned(self):
		basename = f"{frappe.generate_hash(length=8)}.pdf"
		self._index(f"a/{basename}")
		results = controller.lookup_by_basename(basename)
		self.assertEqual(len(results), 1)
		self.assertEqual(results[0]["key"], f"a/{basename}")

	def test_multiple_matches_all_returned(self):
		basename = f"{frappe.generate_hash(length=8)}.pdf"
		self._index(f"a/{basename}")
		self._index(f"b/{basename}")
		results = controller.lookup_by_basename(basename)
		self.assertEqual(len(results), 2)

	def test_bucket_type_filter_narrows_results(self):
		basename = f"{frappe.generate_hash(length=8)}.pdf"
		self._index(f"a/{basename}", bucket_type="private")
		self._index(f"b/{basename}", bucket_type="public")
		results = controller.lookup_by_basename(basename, bucket_type="private")
		self.assertEqual(len(results), 1)
		self.assertEqual(results[0]["bucket_type"], "private")

	def test_empty_basename_short_circuits_without_a_query(self):
		self.assertEqual(controller.lookup_by_basename(""), [])
		self.assertEqual(controller.lookup_by_basename(None), [])


class TestDeriveKeyFromUrl(IntegrationTestCase):
	def test_s3_uri(self):
		self.assertEqual(controller.derive_key_from_url("s3://my-bucket/a/b/c.pdf"), "a/b/c.pdf")

	def test_virtual_hosted_style_url(self):
		self.assertEqual(
			controller.derive_key_from_url("https://my-bucket.s3.ap-south-1.amazonaws.com/a/b/c.pdf"),
			"a/b/c.pdf",
		)

	def test_url_encoded_characters_are_decoded(self):
		self.assertEqual(
			controller.derive_key_from_url("https://my-bucket.s3.amazonaws.com/a%20b/c.pdf"),
			"a b/c.pdf",
		)

	def test_non_url_returns_none(self):
		self.assertIsNone(controller.derive_key_from_url("not-a-url-just-a-filename.pdf"))
		self.assertIsNone(controller.derive_key_from_url(""))
		self.assertIsNone(controller.derive_key_from_url(None))


class TestResolveViaHook(IntegrationTestCase):
	"""
	The read-side counterpart to the app's existing multi_cloud_storage_key_generator
	hook: a site supplies this to interpret its own legacy reference formats. Absence
	must be distinguishable from "found nothing" (None vs []), since callers fall back
	to a plain basename lookup only when no hook is configured at all.
	"""

	def test_no_hook_configured_returns_none(self):
		with patch.object(frappe, "get_hooks", return_value=None):
			self.assertIsNone(controller.resolve_via_hook("anything"))

	def test_hook_result_is_returned_as_is(self):
		candidates = [{"key": "a/b.pdf", "bucket_type": "private"}]
		with (
			patch.object(frappe, "get_hooks", return_value=["some.module.resolver"]),
			patch.object(frappe, "get_attr", return_value=lambda raw_reference: candidates),
		):
			self.assertEqual(controller.resolve_via_hook("legacy-ref"), candidates)

	def test_hook_exception_is_caught_and_logged_not_raised(self):
		def boom(raw_reference):
			raise RuntimeError("resolver blew up")

		with (
			patch.object(frappe, "get_hooks", return_value=["some.module.resolver"]),
			patch.object(frappe, "get_attr", return_value=boom),
			patch.object(frappe, "log_error") as mock_log_error,
		):
			result = controller.resolve_via_hook("legacy-ref")
		self.assertEqual(result, [])
		mock_log_error.assert_called_once()


class TestDefaultAsAttachment(IntegrationTestCase):
	"""
	Content-Disposition default: images/PDF/media preview inline (browser widgets),
	everything else (CSV exports, XML, archives, ...) forces a download. This is what
	lets existing file_url values -- generated long before as_attachment existed --
	behave correctly with no backfill needed.
	"""

	def test_image_defaults_to_inline(self):
		self.assertFalse(controller._default_as_attachment("photo.jpg"))
		self.assertFalse(controller._default_as_attachment("photo.PNG"))

	def test_pdf_defaults_to_inline(self):
		self.assertFalse(controller._default_as_attachment("statement.pdf"))

	def test_csv_defaults_to_attachment(self):
		self.assertTrue(controller._default_as_attachment("export.csv"))

	def test_unknown_extension_defaults_to_attachment(self):
		self.assertTrue(controller._default_as_attachment("data.xlsx"))
		self.assertTrue(controller._default_as_attachment("archive.zip"))

	def test_no_file_name_defaults_to_attachment(self):
		self.assertTrue(controller._default_as_attachment(None))
		self.assertTrue(controller._default_as_attachment(""))


class TestParseAsAttachment(IntegrationTestCase):
	def test_explicit_true_values_win_over_the_extension_default(self):
		self.assertTrue(controller._parse_as_attachment("1", "photo.jpg"))
		self.assertTrue(controller._parse_as_attachment("true", "photo.jpg"))
		self.assertTrue(controller._parse_as_attachment("yes", "photo.jpg"))

	def test_explicit_false_values_win_over_the_extension_default(self):
		self.assertFalse(controller._parse_as_attachment("0", "export.csv"))
		self.assertFalse(controller._parse_as_attachment("false", "export.csv"))

	def test_missing_value_falls_back_to_the_extension_default(self):
		self.assertFalse(controller._parse_as_attachment(None, "photo.jpg"))
		self.assertTrue(controller._parse_as_attachment(None, "export.csv"))
		self.assertFalse(controller._parse_as_attachment("", "photo.jpg"))


class TestGenerateFile(IntegrationTestCase):
	"""
	generate_file is the redirect every private file_url points at. It must pass the
	resolved as_attachment value through to the backend rather than just hardcoding
	inline or attachment -- that's the whole point of threading the flag through.
	"""

	def test_redirects_with_inline_default_for_image(self):
		fake_backend = _FakeBackend()
		with patch.object(controller, "get_backend", lambda: fake_backend):
			controller.generate_file(key="private:a/photo.jpg", file_name="photo.jpg")
		self.assertEqual(fake_backend.get_url_calls, [("a/photo.jpg", "photo.jpg", "private", False)])
		self.assertEqual(frappe.local.response["type"], "redirect")

	def test_redirects_with_attachment_default_for_csv(self):
		fake_backend = _FakeBackend()
		with patch.object(controller, "get_backend", lambda: fake_backend):
			controller.generate_file(key="private:a/export.csv", file_name="export.csv")
		self.assertEqual(fake_backend.get_url_calls, [("a/export.csv", "export.csv", "private", True)])

	def test_explicit_as_attachment_overrides_the_default(self):
		fake_backend = _FakeBackend()
		with patch.object(controller, "get_backend", lambda: fake_backend):
			controller.generate_file(key="private:a/photo.jpg", file_name="photo.jpg", as_attachment="1")
		self.assertEqual(fake_backend.get_url_calls, [("a/photo.jpg", "photo.jpg", "private", True)])

	def test_no_backend_throws(self):
		with patch.object(controller, "get_backend", lambda: None):
			with self.assertRaises(frappe.ValidationError):
				controller.generate_file(key="private:a/b.pdf", file_name="b.pdf")
