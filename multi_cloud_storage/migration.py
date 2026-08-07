# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import os
from urllib.parse import quote

import frappe

from . import controller

BATCH_SIZE = 200
JOB_TIMEOUT = 3600
MAX_STORED_ERRORS = 50

ACTIVE_STATUSES = ("Queued", "In Progress", "Cancelling")
TERMINAL_STATUSES = ("Completed", "Completed with Errors", "Failed", "Cancelled")


def start_migration():
	log = frappe.new_doc("Cloud Storage Migration Log")
	log.status = "Queued"
	log.batch_size = BATCH_SIZE
	log.cursor = ""
	log.started_by = frappe.session.user
	log.started_on = frappe.utils.now_datetime()
	log.insert(ignore_permissions=True)
	_enqueue_batch(log.name)
	return log.name


def cancel_migration(migration_log):
	log = frappe.get_doc("Cloud Storage Migration Log", migration_log)
	if log.status in ("Queued", "In Progress"):
		log.status = "Cancelling"
		log.save(ignore_permissions=True)
	return log.status


def run_batch(migration_log):
	log = frappe.get_doc("Cloud Storage Migration Log", migration_log)
	if log.status in TERMINAL_STATUSES:
		return
	if log.status == "Cancelling":
		_finalize(log, "Cancelled")
		return
	if log.status == "Queued":
		log.status = "In Progress"
	log.current_batch_number = (log.current_batch_number or 0) + 1

	config = controller.get_config()
	if not config:
		_append_error(log, None, "MultiCloud Storage is not enabled")
		_finalize(log, "Failed")
		return

	# `content_hash` cannot be used to detect "already migrated" -- Frappe core populates it
	# with a real content-dedup hash on virtually every file (File.generate_content_hash),
	# unrelated to this app's reuse of the same column to store its own "private:<key>" /
	# "public:<key>" marker after upload. Candidate selection instead mirrors the original
	# _is_local_file_url() check: only rows whose file_url still points at local disk.
	batch_size = log.batch_size or BATCH_SIZE
	rows = frappe.db.sql(
		"""
		SELECT name, file_url, file_name, is_private, attached_to_doctype, attached_to_name
		FROM `tabFile`
		WHERE is_folder = 0
		  AND (file_url LIKE '/files/%%' OR file_url LIKE '/private/files/%%')
		  AND name > %s
		ORDER BY name ASC
		LIMIT %s
		""",
		(log.cursor or "", batch_size),
		as_dict=True,
	)

	for row in rows:
		log.cursor = row["name"]
		_process_row(log, row, config)

	log.save(ignore_permissions=True)
	frappe.db.commit()
	_publish_progress(log)

	if len(rows) < batch_size:
		status = "Completed with Errors" if log.skipped_other else "Completed"
		_finalize(log, status)
		return

	_enqueue_batch(log.name)


def _process_row(log, row, config):
	file_url = row.get("file_url")
	if not file_url or controller._is_cloud_file_url(file_url):
		log.skipped_no_url_or_cloud = (log.skipped_no_url_or_cloud or 0) + 1
		return
	if not controller._is_local_file_url(file_url):
		log.skipped_not_local_url = (log.skipped_not_local_url or 0) + 1
		return
	if controller._is_excluded_extension(row.get("file_name"), config):
		log.skipped_excluded_extension = (log.skipped_excluded_extension or 0) + 1
		return
	try:
		doc = frappe.get_doc("File", row["name"])
		result = _migrate_one_file(doc, config)
		if result is True:
			log.migrated = (log.migrated or 0) + 1
		elif result == "file_not_found":
			log.skipped_file_not_found = (log.skipped_file_not_found or 0) + 1
		else:
			log.skipped_other = (log.skipped_other or 0) + 1
	except Exception as e:
		log.skipped_other = (log.skipped_other or 0) + 1
		_append_error(log, row["name"], str(e))
		frappe.log_error(
			title=f"MultiCloud Storage migrate: {row['name']}",
			message=frappe.get_traceback(),
		)


def _migrate_one_file(doc, config):
	backend = controller.get_backend(config)
	if not backend:
		return False
	path = (doc.file_url or "").strip()
	if not controller._is_local_file_url(path):
		return False
	if path.startswith("/private/files/"):
		relative = path[len("/private/files/") :].lstrip("/")
		file_path = frappe.utils.get_files_path(*relative.split("/"), is_private=True)
	else:
		relative = path[len("/files/") :].lstrip("/")
		file_path = frappe.utils.get_files_path(*relative.split("/"))
	if not os.path.isfile(file_path):
		return "file_not_found"
	parent_doctype = doc.attached_to_doctype or "File"
	parent_name = doc.attached_to_name or ""
	if hasattr(backend, "key_generator"):
		key = backend.key_generator(doc.file_name, parent_doctype, parent_name)
	else:
		key = f"{parent_doctype}/{doc.file_name}"
	content_type = controller._get_content_type(file_path)
	backend.upload(file_path, key, content_type, doc.is_private, doc.file_name)
	prefix = controller.CONTENT_HASH_PRIVATE if doc.is_private else controller.CONTENT_HASH_PUBLIC
	content_hash = prefix + key
	if doc.is_private:
		file_url = f"/api/method/multi_cloud_storage.controller.generate_file?key={quote(content_hash)}&file_name={quote(doc.file_name or '')}"
	else:
		file_url = backend.get_public_url(key) if hasattr(backend, "get_public_url") else doc.file_url
	try:
		os.remove(file_path)
	except OSError:
		pass
	frappe.db.sql(
		"""UPDATE `tabFile` SET file_url=%s, folder=%s, old_parent=%s, content_hash=%s
		WHERE name=%s""",
		(file_url, "Home/Attachments", "Home/Attachments", content_hash, doc.name),
	)
	return True


def _append_error(log, file_name, message):
	errors = frappe.parse_json(log.errors) if log.errors else []
	errors.append({"file": file_name, "error": message})
	log.errors = frappe.as_json(errors[-MAX_STORED_ERRORS:])


def _finalize(log, status):
	log.status = status
	log.ended_on = frappe.utils.now_datetime()
	log.save(ignore_permissions=True)
	frappe.db.commit()
	_publish_progress(log)


def _publish_progress(log):
	frappe.publish_realtime(
		"cloud_storage_migration_progress",
		{
			"name": log.name,
			"status": log.status,
			"current_batch_number": log.current_batch_number,
			"migrated": log.migrated or 0,
			"skipped_no_url_or_cloud": log.skipped_no_url_or_cloud or 0,
			"skipped_not_local_url": log.skipped_not_local_url or 0,
			"skipped_file_not_found": log.skipped_file_not_found or 0,
			"skipped_excluded_extension": log.skipped_excluded_extension or 0,
			"skipped_other": log.skipped_other or 0,
		},
		doctype="Cloud Storage Migration Log",
		docname=log.name,
	)


def _enqueue_batch(migration_log):
	frappe.enqueue(
		"multi_cloud_storage.migration.run_batch",
		queue="long",
		timeout=JOB_TIMEOUT,
		migration_log=migration_log,
		enqueue_after_commit=True,
	)
