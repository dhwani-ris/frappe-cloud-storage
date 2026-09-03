# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import os
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from multi_cloud_storage import controller, migration


class FakeBackend:
	"""In-memory stand-in for a real cloud backend. Never touches the network."""

	def __init__(self):
		self.uploaded = []
		self.objects = {"private": {}, "public": {}}

	def key_generator(self, file_name, parent_doctype, parent_name):
		return f"{parent_doctype}/{parent_name}/{file_name}"

	def upload(self, file_path, key, content_type, is_private, file_name=None):
		self.uploaded.append(key)

	def get_public_url(self, key):
		return f"https://fake-cloud.example.com/{key}"

	def delete(self, key, bucket_type="private"):
		pass

	def object_exists(self, key, bucket_type="private"):
		return key in self.objects.get(bucket_type, {})

	def put_object(self, key, bucket_type="private", size=1, last_modified=None):
		"""Test helper: seed an object as if it were already sitting in the bucket."""
		self.objects.setdefault(bucket_type, {})[key] = (size, last_modified)

	def list_keys_page(self, bucket_type="private", prefix=None, continuation_token=None, page_size=1000):
		items = sorted(self.objects.get(bucket_type, {}).items())
		start = int(continuation_token) if continuation_token else 0
		page_items = items[start : start + page_size]
		next_start = start + len(page_items)
		is_truncated = next_start < len(items)
		return {
			"objects": [(key, size, last_modified) for key, (size, last_modified) in page_items],
			"continuation_token": str(next_start) if is_truncated else None,
			"is_truncated": is_truncated,
		}

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
		self._index_rows = []
		self._attach_targets = []
		self._test_users = []

	def tearDown(self):
		with patch.object(controller, "get_backend", lambda cfg=None: FakeBackend()):
			for name in self._file_docs:
				try:
					frappe.delete_doc(
						"File", name, force=True, ignore_permissions=True, delete_permanently=True
					)
				except Exception:
					pass
			frappe.db.delete(
				"Cloud Storage Reconciliation Issue", {"migration_log": ["in", self._migration_logs or [""]]}
			)
			frappe.db.delete("Cloud Storage Object Index", {"scan_log": ["in", self._migration_logs or [""]]})
			for name in self._migration_logs:
				try:
					frappe.delete_doc(
						"Cloud Storage Migration Log", name, force=True, ignore_permissions=True
					)
				except Exception:
					pass
			for name in self._index_rows:
				try:
					frappe.delete_doc("Cloud Storage Object Index", name, force=True, ignore_permissions=True)
				except Exception:
					pass
			for name in self._attach_targets:
				try:
					frappe.delete_doc(
						"Cloud Storage Attach Field Target", name, force=True, ignore_permissions=True
					)
				except Exception:
					pass
			for name in self._test_users:
				try:
					frappe.delete_doc("User", name, force=True, ignore_permissions=True)
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

	def _skip_if_site_has_existing_local_files(self):
		"""_run_upload_batch's query (SELECT ... FROM tabFile WHERE is_folder=0 AND
		file_url LIKE '/files/%' OR '/private/files/%' ... LIMIT batch_size) is
		deliberately unscoped -- in production it must sweep every File on the site.
		Batch-count/pagination assertions in this class only hold on a site with no
		other matching File rows (e.g. CI's fresh test_site); skip rather than assert
		against counts this site's pre-existing data would silently change.
		"""
		existing = frappe.db.count(
			"File",
			{"is_folder": 0, "file_url": ["like", "/files/%"]},
		) + frappe.db.count(
			"File",
			{"is_folder": 0, "file_url": ["like", "/private/files/%"]},
		)
		if existing:
			self.skipTest(
				f"Site already has {existing} File row(s) matching the unscoped Upload "
				"Local Files query; this test's batch/count assertions only hold against "
				"a site with none (e.g. CI's fresh test_site)."
			)

	def _run_sync(self, cancel_before_batch=None, migration_type="Upload Local Files", bucket_type=None):
		"""Drives the enqueue -> run_batch -> re-enqueue chain synchronously in-process."""
		state = {"n": 0}

		def fake_enqueue(migration_log):
			state["n"] += 1
			if cancel_before_batch and state["n"] == cancel_before_batch:
				migration.cancel_migration(migration_log)
			migration.run_batch(migration_log)

		with patch.object(migration, "_enqueue_batch", fake_enqueue):
			log_name = migration.start_migration(migration_type=migration_type, bucket_type=bucket_type)
		self._migration_logs.append(log_name)
		return log_name

	def _make_index_row(self, key, bucket_type="private", scan_log=None):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Object Index",
				"bucket_type": bucket_type,
				"object_key": key,
				"basename": os.path.basename(key),
				"scan_log": scan_log,
			}
		).insert(ignore_permissions=True)
		self._index_rows.append(doc.name)
		return doc

	def _make_attach_field_target(self, target_doctype="User", target_fieldname="user_image", **kwargs):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Attach Field Target",
				"target_doctype": target_doctype,
				"target_fieldname": target_fieldname,
				"bucket_type": "private",
				**kwargs,
			}
		).insert(ignore_permissions=True)
		self._attach_targets.append(doc.name)
		return doc

	def _make_test_user(self, user_image):
		email = f"{frappe.generate_hash(length=8)}@example.com"
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Test",
				"user_image": user_image,
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		self._test_users.append(doc.name)
		return doc

	def test_processes_all_files_across_multiple_batches(self):
		self._skip_if_site_has_existing_local_files()
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
		self._skip_if_site_has_existing_local_files()
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
		self._skip_if_site_has_existing_local_files()
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


class TestScanBucketIndex(IntegrationTestCase):
	"""
	Covers the Scan Bucket Index mode: paginated listing gets bulk-inserted into
	Cloud Storage Object Index, not walked one key at a time -- this is the mechanism
	that makes bulk reconciliation bucket-size-bound instead of row-count-bound.
	"""

	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._migration_logs = []

	def tearDown(self):
		frappe.db.delete("Cloud Storage Object Index", {"scan_log": ["in", self._migration_logs or [""]]})
		for name in self._migration_logs:
			try:
				frappe.delete_doc("Cloud Storage Migration Log", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		self._commit_patch.stop()
		super().tearDown()

	def _run_sync(self, migration_type, bucket_type=None):
		def fake_enqueue(migration_log):
			migration.run_batch(migration_log)

		with patch.object(migration, "_enqueue_batch", fake_enqueue):
			log_name = migration.start_migration(migration_type=migration_type, bucket_type=bucket_type)
		self._migration_logs.append(log_name)
		return log_name

	def test_indexes_every_object_across_multiple_pages(self):
		fake_backend = FakeBackend()
		for i in range(5):
			fake_backend.put_object(f"folder/{i}.pdf", bucket_type="private")

		with (
			patch.object(controller, "get_backend", lambda cfg=None: fake_backend),
			patch.object(controller, "get_config", lambda: _fake_config()),
			patch.object(migration, "SCAN_BATCH_SIZE", 2),
		):
			log_name = self._run_sync("Scan Bucket Index", bucket_type="private")

		log = frappe.get_doc("Cloud Storage Migration Log", log_name)
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.objects_indexed, 5)
		self.assertEqual(log.current_batch_number, 3)

		indexed = frappe.get_all(
			"Cloud Storage Object Index",
			filters={"scan_log": log_name},
			fields=["object_key", "basename", "bucket_type"],
		)
		self.assertEqual(len(indexed), 5)
		self.assertEqual({row.basename for row in indexed}, {f"{i}.pdf" for i in range(5)})
		self.assertTrue(all(row.bucket_type == "private" for row in indexed))

	def test_fresh_scan_replaces_stale_index_for_that_bucket_type(self):
		stale = frappe.get_doc(
			{
				"doctype": "Cloud Storage Object Index",
				"bucket_type": "private",
				"object_key": "old/gone.pdf",
				"basename": "gone.pdf",
			}
		).insert(ignore_permissions=True)

		fake_backend = FakeBackend()
		fake_backend.put_object("new/here.pdf", bucket_type="private")

		try:
			with (
				patch.object(controller, "get_backend", lambda cfg=None: fake_backend),
				patch.object(controller, "get_config", lambda: _fake_config()),
			):
				self._run_sync("Scan Bucket Index", bucket_type="private")
		finally:
			if frappe.db.exists("Cloud Storage Object Index", stale.name):
				frappe.delete_doc(
					"Cloud Storage Object Index", stale.name, force=True, ignore_permissions=True
				)

		remaining_basenames = frappe.get_all(
			"Cloud Storage Object Index", filters={"bucket_type": "private"}, pluck="basename"
		)
		self.assertNotIn("gone.pdf", remaining_basenames)
		self.assertIn("here.pdf", remaining_basenames)


class TestLinkExistingFiles(IntegrationTestCase):
	"""
	Covers Link Existing Objects (Files): resolving a tabFile row against the Cloud
	Storage Object Index (never a live per-row existence check -- see
	link_existing_object(..., verify=False) in controller.py) and routing anything
	that doesn't resolve cleanly to Cloud Storage Reconciliation Issue instead of
	guessing.

	Exercises _resolve_and_link_file_row() directly rather than driving the full
	start_migration -> run_batch engine end to end: the engine's candidate query is
	deliberately unscoped (SELECT ... FROM tabFile WHERE is_folder=0 AND name > cursor,
	no other filter -- that's the whole point in production, where it must sweep every
	File on the site). Run through the full engine on a real site with a non-trivial
	File table, that same lack of scoping sweeps every pre-existing File row into the
	batch alongside this test's own, making aggregate counters like log.migrated /
	log.flagged_unresolved -- and therefore log.status -- depend on unrelated data no
	test should know about. Calling the row-level resolver directly tests the actual
	resolution/linking logic precisely, independent of whatever else happens to be in
	tabFile on whichever site the suite runs against.
	"""

	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._migration_logs = []
		self._file_docs = []
		self._index_rows = []

	def tearDown(self):
		with patch.object(controller, "get_backend", lambda cfg=None: FakeBackend()):
			for name in self._file_docs:
				try:
					frappe.delete_doc(
						"File", name, force=True, ignore_permissions=True, delete_permanently=True
					)
				except Exception:
					pass
		frappe.db.delete(
			"Cloud Storage Reconciliation Issue", {"migration_log": ["in", self._migration_logs or [""]]}
		)
		for name in self._migration_logs:
			try:
				frappe.delete_doc("Cloud Storage Migration Log", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		for name in self._index_rows:
			try:
				frappe.delete_doc("Cloud Storage Object Index", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		self._commit_patch.stop()
		super().tearDown()

	def _make_log(self):
		log = frappe.new_doc("Cloud Storage Migration Log")
		log.status = "In Progress"
		log.migration_type = "Link Existing Objects (Files)"
		log.insert(ignore_permissions=True)
		self._migration_logs.append(log.name)
		return log

	def _row_for(self, file_doc):
		return {"name": file_doc.name, "file_url": file_doc.file_url, "is_private": file_doc.is_private}

	def _make_legacy_file(self, legacy_url, is_private=1):
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": os.path.basename(legacy_url),
				"file_url": legacy_url,
				"is_private": is_private,
				"content_hash": frappe.generate_hash(length=20),
			}
		).insert(ignore_permissions=True)
		self._file_docs.append(doc.name)
		return doc

	def _make_index_row(self, key, bucket_type="private"):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Object Index",
				"bucket_type": bucket_type,
				"object_key": key,
				"basename": os.path.basename(key),
			}
		).insert(ignore_permissions=True)
		self._index_rows.append(doc.name)
		return doc

	def test_auto_links_unique_basename_match(self):
		file_doc = self._make_legacy_file("https://legacy.example.com/old/bucket/receipt-001.pdf")
		self._make_index_row("beam/2026/receipt-001.pdf", bucket_type="private")
		log = self._make_log()

		fake_backend = FakeBackend()
		migration._resolve_and_link_file_row(log, self._row_for(file_doc), fake_backend)

		self.assertEqual(log.migrated, 1)
		self.assertEqual(log.flagged_unresolved, 0)
		self.assertEqual(log.flagged_for_review, 0)

		file_doc.reload()
		self.assertEqual(file_doc.content_hash, "private:beam/2026/receipt-001.pdf")
		self.assertIn("multi_cloud_storage.controller.generate_file", file_doc.file_url)
		self.assertEqual(fake_backend.uploaded, [])

	def test_ambiguous_basename_is_flagged_not_guessed(self):
		file_doc = self._make_legacy_file("https://legacy.example.com/old/receipt-001.pdf")
		self._make_index_row("beam/2025/receipt-001.pdf", bucket_type="private")
		self._make_index_row("beam/2026/receipt-001.pdf", bucket_type="private")
		log = self._make_log()

		fake_backend = FakeBackend()
		migration._resolve_and_link_file_row(log, self._row_for(file_doc), fake_backend)

		self.assertEqual(log.migrated, 0)
		self.assertEqual(log.flagged_for_review, 1)

		file_doc.reload()
		self.assertEqual(file_doc.file_url, "https://legacy.example.com/old/receipt-001.pdf")

		issues = frappe.get_all(
			"Cloud Storage Reconciliation Issue",
			filters={"migration_log": log.name},
			fields=["reason", "source_doctype", "source_name", "action_required"],
		)
		self.assertEqual(len(issues), 1)
		self.assertEqual(issues[0].reason, "Ambiguous")
		self.assertEqual(issues[0].source_doctype, "File")
		self.assertEqual(issues[0].source_name, file_doc.name)
		self.assertIn("beam/2025/receipt-001.pdf", issues[0].action_required)
		self.assertIn("beam/2026/receipt-001.pdf", issues[0].action_required)
		self.assertIn("Resolved Key", issues[0].action_required)

	def test_no_match_is_flagged_unresolved(self):
		file_doc = self._make_legacy_file("https://legacy.example.com/old/nowhere-to-be-found.pdf")
		log = self._make_log()

		fake_backend = FakeBackend()
		migration._resolve_and_link_file_row(log, self._row_for(file_doc), fake_backend)

		self.assertEqual(log.migrated, 0)
		self.assertEqual(log.flagged_unresolved, 1)

		issues = frappe.get_all(
			"Cloud Storage Reconciliation Issue",
			filters={"migration_log": log.name},
			fields=["reason", "action_required"],
		)
		self.assertEqual(len(issues), 1)
		self.assertEqual(issues[0].reason, "Unresolved")
		self.assertIn("Resolved Key", issues[0].action_required)

	def test_already_cloud_reference_is_skipped_not_reflagged(self):
		file_doc = self._make_legacy_file(
			"/api/method/multi_cloud_storage.controller.generate_file?key=private%3Abeam%2Freceipt.pdf"
		)
		log = self._make_log()

		fake_backend = FakeBackend()
		migration._resolve_and_link_file_row(log, self._row_for(file_doc), fake_backend)

		self.assertEqual(log.migrated, 0)
		self.assertEqual(log.skipped_no_url_or_cloud, 1)
		self.assertEqual(log.flagged_unresolved, 0)
		self.assertFalse(frappe.db.exists("Cloud Storage Reconciliation Issue", {"migration_log": log.name}))


class TestLinkExistingAttachFields(IntegrationTestCase):
	"""
	Covers Link Existing Objects (Attach Fields): records whose Attach / Attach Image
	value was populated by direct data import (so there's no tabFile row at all) get a
	File created for them and the parent field repointed -- and require_manual_review
	targets never auto-link even on a single clean match.

	test_creates_file_and_updates_parent_field_on_unique_match and
	test_require_manual_review_never_auto_links_even_on_unique_match call
	_resolve_and_link_attach_row() directly rather than driving the full batch engine
	across the real User table -- see the equivalent note on TestLinkExistingFiles for
	why: on a site with many real Users, _fetch_attach_target_rows() (deliberately
	unscoped by design -- it must walk every record on a real site in production) would
	sweep them all into this test's counters. test_disabled_target_is_never_scanned
	doesn't have that problem -- with zero enabled targets, _run_link_attach_fields_batch
	never queries User at all -- so it exercises that function directly instead.
	"""

	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._migration_logs = []
		self._index_rows = []
		self._attach_targets = []
		self._test_users = []
		self._file_docs = []

	def tearDown(self):
		with patch.object(controller, "get_backend", lambda cfg=None: FakeBackend()):
			for name in self._file_docs:
				try:
					frappe.delete_doc(
						"File", name, force=True, ignore_permissions=True, delete_permanently=True
					)
				except Exception:
					pass
			for name in self._test_users:
				try:
					frappe.delete_doc("User", name, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.delete(
			"Cloud Storage Reconciliation Issue", {"migration_log": ["in", self._migration_logs or [""]]}
		)
		for name in self._migration_logs:
			try:
				frappe.delete_doc("Cloud Storage Migration Log", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		for name in self._index_rows:
			try:
				frappe.delete_doc("Cloud Storage Object Index", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		for name in self._attach_targets:
			try:
				frappe.delete_doc(
					"Cloud Storage Attach Field Target", name, force=True, ignore_permissions=True
				)
			except Exception:
				pass
		self._commit_patch.stop()
		super().tearDown()

	def _make_log(self):
		log = frappe.new_doc("Cloud Storage Migration Log")
		log.status = "In Progress"
		log.migration_type = "Link Existing Objects (Attach Fields)"
		log.insert(ignore_permissions=True)
		self._migration_logs.append(log.name)
		return log

	def _make_index_row(self, key, bucket_type="private"):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Object Index",
				"bucket_type": bucket_type,
				"object_key": key,
				"basename": os.path.basename(key),
			}
		).insert(ignore_permissions=True)
		self._index_rows.append(doc.name)
		return doc

	def _make_target(self, require_manual_review=0):
		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Attach Field Target",
				"target_doctype": "User",
				"target_fieldname": "user_image",
				"bucket_type": "private",
				"require_manual_review": require_manual_review,
			}
		).insert(ignore_permissions=True)
		self._attach_targets.append(doc.name)
		return doc

	def _make_test_user(self, user_image):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"{frappe.generate_hash(length=8)}@example.com",
				"first_name": "Test",
				"user_image": user_image,
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		self._test_users.append(doc.name)
		return doc

	def test_creates_file_and_updates_parent_field_on_unique_match(self):
		target = self._make_target()
		user = self._make_test_user("https://legacy.example.com/old/avatar-1.png")
		self._make_index_row("beam/avatars/avatar-1.png", bucket_type="private")
		log = self._make_log()

		fake_backend = FakeBackend()
		row = {"name": user.name, "raw_value": user.user_image}
		migration._resolve_and_link_attach_row(log, target.as_dict(), row, fake_backend)

		self.assertEqual(log.migrated, 1)
		self.assertEqual(log.flagged_for_review, 0)
		self.assertEqual(log.flagged_unresolved, 0)

		user.reload()
		self.assertIn("multi_cloud_storage.controller.generate_file", user.user_image)

		# Filtered by content_hash (set only by our own _create_file_for_attach_value)
		# rather than just attached_to_doctype/name/field: some Frappe core versions'
		# attach_files_to_document on_update hook auto-attaches a bookkeeping File for
		# ANY non-empty Attach field value regardless of shape, which would otherwise
		# alias into this same (doctype, name, field) triple and make this assertion
		# depend on which core version is installed rather than on our own code.
		created_files = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "User",
				"attached_to_name": user.name,
				"attached_to_field": "user_image",
				"content_hash": "private:beam/avatars/avatar-1.png",
			},
			fields=["name", "file_url", "content_hash"],
		)
		self.assertEqual(len(created_files), 1)
		self._file_docs.append(created_files[0].name)
		self.assertEqual(created_files[0].file_url, user.user_image)

	def test_require_manual_review_never_auto_links_even_on_unique_match(self):
		target = self._make_target(require_manual_review=1)
		user = self._make_test_user("https://legacy.example.com/old/sensitive-doc.png")
		self._make_index_row("beam/sensitive/sensitive-doc.png", bucket_type="private")
		log = self._make_log()

		fake_backend = FakeBackend()
		row = {"name": user.name, "raw_value": user.user_image}
		migration._resolve_and_link_attach_row(log, target.as_dict(), row, fake_backend)

		self.assertEqual(log.migrated, 0)
		self.assertEqual(log.flagged_for_review, 1)

		user.reload()
		self.assertEqual(user.user_image, "https://legacy.example.com/old/sensitive-doc.png")

		issues = frappe.get_all(
			"Cloud Storage Reconciliation Issue",
			filters={"migration_log": log.name},
			fields=["reason", "action_required"],
		)
		self.assertEqual(len(issues), 1)
		self.assertEqual(issues[0].reason, "Manual Review Required")
		self.assertIn("beam/sensitive/sensitive-doc.png", issues[0].action_required)
		self.assertIn("Resolved Key", issues[0].action_required)

	def test_disabled_target_is_never_scanned(self):
		self._make_target()
		frappe.db.set_value("Cloud Storage Attach Field Target", self._attach_targets[-1], "enabled", 0)
		user = self._make_test_user("https://legacy.example.com/old/avatar-2.png")
		log = self._make_log()

		fake_backend = FakeBackend()
		with patch.object(controller, "get_backend", lambda cfg=None: fake_backend):
			is_last_batch = migration._run_link_attach_fields_batch(log, _fake_config())

		self.assertTrue(is_last_batch)
		self.assertEqual(log.migrated, 0)

		user.reload()
		self.assertEqual(user.user_image, "https://legacy.example.com/old/avatar-2.png")


class TestBatchJobFailureRecovery(IntegrationTestCase):
	"""
	Covers the crash-recovery path registered as on_failure on every batch's background
	job (see _enqueue_batch / handle_batch_job_failure / recover_or_fail_migration).

	Motivating bug: a batch's background job died with "Lost connection to server during
	query" / "Server has gone away" while saving at the end of run_batch(). run_batch()
	never reached _finalize(), and Frappe's own execute_job() wrapper then failed too --
	it tried frappe.db.rollback() on that same dead connection and raised again -- so
	nothing ever moved the Migration Log off "In Progress" and nothing ever retried it.
	RQ invokes on_failure for any crashed job unconditionally (regardless of what killed
	it), which is what recover_or_fail_migration hangs off of.
	"""

	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._migration_logs = []

	def tearDown(self):
		for name in self._migration_logs:
			try:
				frappe.delete_doc("Cloud Storage Migration Log", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		self._commit_patch.stop()
		super().tearDown()

	def _make_log(self, status="In Progress", retry_count=0, cursor="some-cursor"):
		log = frappe.get_doc(
			{
				"doctype": "Cloud Storage Migration Log",
				"status": status,
				"retry_count": retry_count,
				"cursor": cursor,
			}
		).insert(ignore_permissions=True)
		self._migration_logs.append(log.name)
		return log

	def test_crash_below_retry_limit_requeues_from_last_cursor(self):
		log = self._make_log(retry_count=1)

		enqueued = []
		with patch.object(migration, "_enqueue_batch", enqueued.append):
			migration.recover_or_fail_migration(log.name, "OperationalError: Server has gone away")

		log.reload()
		self.assertEqual(log.status, "Queued")
		self.assertEqual(log.retry_count, 2)
		self.assertEqual(log.cursor, "some-cursor")
		self.assertEqual(enqueued, [log.name])
		errors = frappe.parse_json(log.errors)
		self.assertIn("Server has gone away", errors[-1]["error"])

	def test_crash_at_retry_limit_marks_failed_and_stops_retrying(self):
		log = self._make_log(retry_count=migration.MAX_AUTO_RETRIES)

		enqueued = []
		with patch.object(migration, "_enqueue_batch", enqueued.append):
			migration.recover_or_fail_migration(log.name, "OperationalError: Server has gone away")

		log.reload()
		self.assertEqual(log.status, "Failed")
		self.assertIsNotNone(log.ended_on)
		self.assertEqual(log.retry_count, migration.MAX_AUTO_RETRIES)
		self.assertEqual(enqueued, [])

	def test_already_terminal_log_is_left_alone(self):
		log = self._make_log(status="Completed")

		enqueued = []
		with patch.object(migration, "_enqueue_batch", enqueued.append):
			migration.recover_or_fail_migration(log.name, "should not matter")

		log.reload()
		self.assertEqual(log.status, "Completed")
		self.assertFalse(log.errors)
		self.assertEqual(enqueued, [])

	def test_unknown_migration_log_is_a_noop(self):
		migration.recover_or_fail_migration("MIGRATION-does-not-exist", "boom")

	def test_handle_batch_job_failure_extracts_kwargs_and_delegates(self):
		"""Thin plumbing wrapper: verifies the job.kwargs shape frappe.enqueue() actually
		produces (site + nested method kwargs) is unpacked correctly, without touching
		frappe.init/connect/destroy -- those would tear down this test's own connection.
		"""
		log = self._make_log()
		job = SimpleNamespace(kwargs={"site": frappe.local.site, "kwargs": {"migration_log": log.name}})

		calls = []
		with (
			patch.object(migration, "recover_or_fail_migration", lambda name, msg: calls.append((name, msg))),
			patch.object(frappe, "init", lambda *a, **kw: None),
			patch.object(frappe, "connect", lambda *a, **kw: None),
			patch.object(frappe, "destroy", lambda: None),
		):
			migration.handle_batch_job_failure(job, None, RuntimeError, RuntimeError("lost connection"), None)

		self.assertEqual(calls, [(log.name, "RuntimeError: lost connection")])

	def test_handle_batch_job_failure_ignores_malformed_job_kwargs(self):
		with patch.object(migration, "recover_or_fail_migration") as mock_recover:
			migration.handle_batch_job_failure(
				SimpleNamespace(kwargs={}), None, RuntimeError, RuntimeError(), None
			)

		mock_recover.assert_not_called()

	def test_enqueue_batch_registers_failure_callback(self):
		captured = {}
		with patch.object(frappe, "enqueue", lambda *a, **kw: captured.update(kw)):
			migration._enqueue_batch("MIGRATION-0001")

		self.assertEqual(captured.get("on_failure"), "multi_cloud_storage.migration.handle_batch_job_failure")


class TestBatchSizeDefaults(IntegrationTestCase):
	"""
	Covers the per-migration-type batch size split: Upload Local Files does a real
	per-row upload to the bucket and stays small (BATCH_SIZE); Link Existing Objects
	(both modes) only resolves against the already-scanned Cloud Storage Object Index
	and does a plain UPDATE -- no per-row network call -- so it can run a much larger
	batch (LINK_BATCH_SIZE) without approaching the background job's timeout. Scan
	Bucket Index is unaffected (SCAN_BATCH_SIZE, one list-objects page per batch).
	"""

	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._migration_logs = []

	def tearDown(self):
		for name in self._migration_logs:
			try:
				frappe.delete_doc("Cloud Storage Migration Log", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		self._commit_patch.stop()
		super().tearDown()

	def _start(self, migration_type, **kwargs):
		with patch.object(migration, "_enqueue_batch", lambda *a, **kw: None):
			log_name = migration.start_migration(migration_type=migration_type, **kwargs)
		self._migration_logs.append(log_name)
		return frappe.get_doc("Cloud Storage Migration Log", log_name)

	def test_upload_local_files_keeps_the_small_batch_size(self):
		log = self._start("Upload Local Files")
		self.assertEqual(log.batch_size, migration.BATCH_SIZE)

	def test_scan_bucket_index_keeps_its_own_batch_size(self):
		log = self._start("Scan Bucket Index", bucket_type="private")
		self.assertEqual(log.batch_size, migration.SCAN_BATCH_SIZE)

	def test_link_existing_files_gets_the_larger_batch_size(self):
		log = self._start("Link Existing Objects (Files)")
		self.assertEqual(log.batch_size, migration.LINK_BATCH_SIZE)

	def test_link_existing_attach_fields_gets_the_larger_batch_size(self):
		log = self._start("Link Existing Objects (Attach Fields)")
		self.assertEqual(log.batch_size, migration.LINK_BATCH_SIZE)

	def test_defaults_are_looked_up_live_not_frozen_at_import(self):
		"""Regression guard: start_migration() must read BATCH_SIZE/SCAN_BATCH_SIZE/
		LINK_BATCH_SIZE as bare names at call time, not bake them into a dict built once
		at import time -- otherwise patch.object(migration, "SCAN_BATCH_SIZE", ...), used
		throughout this test module (e.g. TestScanBucketIndex), silently stops working.
		"""
		with (
			patch.object(migration, "BATCH_SIZE", 7),
			patch.object(migration, "SCAN_BATCH_SIZE", 8),
			patch.object(migration, "LINK_BATCH_SIZE", 9),
		):
			self.assertEqual(self._start("Upload Local Files").batch_size, 7)
			self.assertEqual(self._start("Scan Bucket Index", bucket_type="private").batch_size, 8)
			self.assertEqual(self._start("Link Existing Objects (Files)").batch_size, 9)


class TestResumeMigration(IntegrationTestCase):
	"""
	Covers resume_migration(): the supported way to recover a run that hasn't self-healed
	through the normal lifecycle -- e.g. one stuck "In Progress" because its background
	job crashed before this app's crash-recovery was deployed (see
	TestBatchJobFailureRecovery), or one that hit MAX_AUTO_RETRIES and was marked Failed
	while the underlying cause has since cleared. Always resumes from the last saved
	cursor rather than starting over.
	"""

	def setUp(self):
		super().setUp()
		self._commit_patch = patch.object(frappe.db, "commit", lambda *a, **kw: None)
		self._commit_patch.start()
		self._migration_logs = []

	def tearDown(self):
		for name in self._migration_logs:
			try:
				frappe.delete_doc("Cloud Storage Migration Log", name, force=True, ignore_permissions=True)
			except Exception:
				pass
		self._commit_patch.stop()
		super().tearDown()

	def _make_log(self, status, retry_count=0, cursor="some-cursor"):
		log = frappe.get_doc(
			{
				"doctype": "Cloud Storage Migration Log",
				"status": status,
				"retry_count": retry_count,
				"cursor": cursor,
			}
		).insert(ignore_permissions=True)
		self._migration_logs.append(log.name)
		return log

	def test_resume_from_failed_requeues_from_last_cursor_and_resets_retries(self):
		log = self._make_log(status="Failed", retry_count=migration.MAX_AUTO_RETRIES)

		enqueued = []
		with patch.object(migration, "_enqueue_batch", enqueued.append):
			result = migration.resume_migration(log.name)

		log.reload()
		self.assertEqual(result, "Queued")
		self.assertEqual(log.status, "Queued")
		self.assertEqual(log.retry_count, 0)
		self.assertEqual(log.cursor, "some-cursor")
		self.assertEqual(enqueued, [log.name])

	def test_resume_from_orphaned_in_progress_is_allowed(self):
		"""The exact shape of the production incident: a run stuck "In Progress" with no
		worker actually processing it any more."""
		log = self._make_log(status="In Progress")

		enqueued = []
		with patch.object(migration, "_enqueue_batch", enqueued.append):
			migration.resume_migration(log.name)

		log.reload()
		self.assertEqual(log.status, "Queued")
		self.assertEqual(enqueued, [log.name])

	def test_resume_from_completed_is_rejected(self):
		log = self._make_log(status="Completed")

		with patch.object(migration, "_enqueue_batch") as mock_enqueue:
			with self.assertRaises(frappe.ValidationError):
				migration.resume_migration(log.name)
		mock_enqueue.assert_not_called()

		log.reload()
		self.assertEqual(log.status, "Completed")

	def test_resume_from_cancelled_is_rejected(self):
		log = self._make_log(status="Cancelled")

		with patch.object(migration, "_enqueue_batch") as mock_enqueue:
			with self.assertRaises(frappe.ValidationError):
				migration.resume_migration(log.name)
		mock_enqueue.assert_not_called()

	def test_cancel_and_resume_are_whitelisted(self):
		"""Both are invoked from the Desk form via frappe.call() (see
		cloud_storage_migration_log.js) -- without @frappe.whitelist() they 404 on click."""
		self.assertIn(migration.cancel_migration, frappe.whitelisted)
		self.assertIn(migration.resume_migration, frappe.whitelisted)
