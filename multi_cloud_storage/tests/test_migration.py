# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import os
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from multi_cloud_storage import controller, migration


class FakeBackend:
	"""In-memory stand-in for a real cloud backend. Never touches the network."""

	def __init__(self):
		self.uploaded = []

	def key_generator(self, file_name, parent_doctype, parent_name):
		return f"{parent_doctype}/{parent_name}/{file_name}"

	def upload(self, file_path, key, content_type, is_private, file_name=None):
		self.uploaded.append(key)

	def get_public_url(self, key):
		return f"https://fake-cloud.example.com/{key}"

	def delete(self, key, bucket_type="private"):
		pass

	def test_connection(self):
		return True, None


def _fake_config(excluded_file_extensions=""):
	return frappe._dict(
		enabled=1,
		storage_provider="Amazon S3",
		excluded_file_extensions=excluded_file_extensions,
	)


class TestMigration(IntegrationTestCase):
	"""
	Covers the chunked background migration engine (multi_cloud_storage/migration.py):
	keyset-paginated batching, the duplicate-run guard, mid-run cancellation, and the
	configurable excluded-file-extensions filter. Uses a FakeBackend so no test ever
	makes a real cloud API call, and neutralises frappe.db.commit() for the duration
	of each test so the migration engine's real (necessary) per-batch commits never
	durably land on a shared site's database.
	"""

	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._file_paths = []
		self._file_docs = []
		self._migration_logs = []

	def tearDown(self):
		# Frappe's test runner only rolls back at the END of the whole test class, not
		# after each test method, so every doc created here must be explicitly deleted --
		# otherwise it leaks into later tests in this class (e.g. as a permanently-local
		# File row that pollutes the next test's candidate query). get_backend is faked
		# here too so File's on_trash -> delete_from_cloud hook never reaches a real backend.
		with patch.object(controller, "get_backend", lambda cfg=None: FakeBackend()):
			for name in self._file_docs:
				try:
					frappe.delete_doc("File", name, force=True, ignore_permissions=True, delete_permanently=True)
				except Exception:
					pass
			for name in self._migration_logs:
				try:
					frappe.delete_doc(
						"Cloud Storage Migration Log", name, force=True, ignore_permissions=True
					)
				except Exception:
					pass
		for path in self._file_paths:
			try:
				if os.path.isfile(path):
					os.remove(path)
			except OSError:
				pass
		self._commit_patch.stop()
		super().tearDown()

	def _make_file(self, content=None, file_name=None, is_private=0):
		# Content must be unique per file -- Frappe's own File.validate_duplicate_entry()
		# dedupes by content hash and silently reuses one physical file across records
		# with identical bytes, which would make this helper's callers step on each other.
		content = content if content is not None else f"content-{frappe.generate_hash(length=16)}".encode()
		file_name = file_name or f"{frappe.generate_hash(length=8)}.txt"
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"content": content,
				"is_private": is_private,
			}
		).insert(ignore_permissions=True)
		self._file_docs.append(doc.name)
		relative = doc.file_url.split("/files/")[-1]
		self._file_paths.append(frappe.utils.get_files_path(relative, is_private=bool(is_private)))
		return doc

	def _run_sync(self, cancel_before_batch=None):
		"""Drives the enqueue -> run_batch -> re-enqueue chain synchronously in-process."""
		state = {"n": 0}

		def fake_enqueue(migration_log):
			state["n"] += 1
			if cancel_before_batch and state["n"] == cancel_before_batch:
				migration.cancel_migration(migration_log)
			migration.run_batch(migration_log)

		with patch.object(migration, "_enqueue_batch", fake_enqueue):
			log_name = migration.start_migration()
		self._migration_logs.append(log_name)
		return log_name

	def test_processes_all_files_across_multiple_batches(self):
		# Files are created while cloud storage looks disabled, so the real-time
		# file_upload_to_cloud hook is a no-op and they stay "existing local files" —
		# exactly the backlog migrate_existing_files is meant to pick up.
		with patch.object(controller, "get_config", lambda: None):
			for _ in range(5):
				self._make_file()

		fake_backend = FakeBackend()
		with (
			patch.object(controller, "get_backend", lambda cfg=None: fake_backend),
			patch.object(controller, "get_config", lambda: _fake_config()),
			patch.object(migration, "BATCH_SIZE", 2),
		):
			log_name = self._run_sync()

		log = frappe.get_doc("Cloud Storage Migration Log", log_name)
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.migrated, 5)
		self.assertEqual(log.current_batch_number, 3)
		self.assertEqual(len(fake_backend.uploaded), 5)

	def test_file_missing_from_disk_is_skipped_and_run_terminates(self):
		# The File record survives (e.g. a manual disk cleanup or a restore mismatch)
		# but the bytes are gone. That must be counted and skipped, not crash the batch
		# or get stuck retrying it forever.
		with patch.object(controller, "get_config", lambda: None):
			missing_doc = self._make_file()
			missing_path = frappe.utils.get_files_path(missing_doc.file_url.split("/files/")[-1])
			os.remove(missing_path)
			self._file_paths.remove(missing_path)
			for _ in range(3):
				self._make_file()

		fake_backend = FakeBackend()
		with (
			patch.object(controller, "get_backend", lambda cfg=None: fake_backend),
			patch.object(controller, "get_config", lambda: _fake_config()),
			patch.object(migration, "BATCH_SIZE", 2),
		):
			log_name = self._run_sync()

		log = frappe.get_doc("Cloud Storage Migration Log", log_name)
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.migrated, 3)
		self.assertEqual(log.skipped_file_not_found, 1)

	def test_excluded_extension_is_never_uploaded(self):
		with patch.object(controller, "get_config", lambda: None):
			self._make_file(file_name="secret.cer")
			self._make_file(file_name="normal.txt")

		fake_backend = FakeBackend()
		with (
			patch.object(controller, "get_backend", lambda cfg=None: fake_backend),
			patch.object(controller, "get_config", lambda: _fake_config(excluded_file_extensions=".cer")),
		):
			log_name = self._run_sync()

		log = frappe.get_doc("Cloud Storage Migration Log", log_name)
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.migrated, 1)
		self.assertEqual(log.skipped_excluded_extension, 1)
		self.assertTrue(all(not key.endswith(".cer") for key in fake_backend.uploaded))

	def test_duplicate_run_is_blocked(self):
		with (
			patch.object(controller, "get_config", lambda: _fake_config()),
			patch.object(controller, "get_backend", lambda cfg=None: FakeBackend()),
		):
			log = frappe.get_doc({"doctype": "Cloud Storage Migration Log", "status": "In Progress"}).insert(
				ignore_permissions=True
			)
			self._migration_logs.append(log.name)
			with self.assertRaises(frappe.ValidationError):
				controller.migrate_existing_files()

	def test_cancel_stops_before_next_batch(self):
		with patch.object(controller, "get_config", lambda: None):
			for _ in range(4):
				self._make_file()

		fake_backend = FakeBackend()
		with (
			patch.object(controller, "get_backend", lambda cfg=None: fake_backend),
			patch.object(controller, "get_config", lambda: _fake_config()),
			patch.object(migration, "BATCH_SIZE", 1),
		):
			log_name = self._run_sync(cancel_before_batch=2)

		log = frappe.get_doc("Cloud Storage Migration Log", log_name)
		self.assertEqual(log.status, "Cancelled")
		self.assertEqual(log.migrated, 1)
		self.assertEqual(log.current_batch_number, 1)

	def test_missing_config_marks_run_failed(self):
		with patch.object(controller, "get_config", lambda: None):
			log = frappe.get_doc({"doctype": "Cloud Storage Migration Log", "status": "Queued"}).insert(
				ignore_permissions=True
			)
			self._migration_logs.append(log.name)
			migration.run_batch(log.name)

		log.reload()
		self.assertEqual(log.status, "Failed")
		self.assertIsNotNone(log.ended_on)
