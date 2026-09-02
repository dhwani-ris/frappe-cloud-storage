# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import os
from urllib.parse import unquote

import frappe

from . import controller

BATCH_SIZE = 200
SCAN_BATCH_SIZE = 1000
JOB_TIMEOUT = 3600
MAX_STORED_ERRORS = 50

ACTIVE_STATUSES = ("Queued", "In Progress", "Cancelling")
TERMINAL_STATUSES = ("Completed", "Completed with Errors", "Failed", "Cancelled")

MIGRATION_TYPES = (
	"Upload Local Files",
	"Scan Bucket Index",
	"Link Existing Objects (Files)",
	"Link Existing Objects (Attach Fields)",
)


def start_migration(migration_type="Upload Local Files", bucket_type=None):
	log = frappe.new_doc("Cloud Storage Migration Log")
	log.status = "Queued"
	log.migration_type = migration_type
	log.batch_size = SCAN_BATCH_SIZE if migration_type == "Scan Bucket Index" else BATCH_SIZE
	log.bucket_type = bucket_type
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

	handler = _BATCH_HANDLERS.get(log.migration_type or "Upload Local Files", _run_upload_batch)
	is_last_batch = handler(log, config)

	log.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep
	_publish_progress(log)

	if is_last_batch:
		has_issues = log.skipped_other or log.flagged_for_review or log.flagged_unresolved
		status = "Completed with Errors" if has_issues else "Completed"
		_finalize(log, status)
		return

	_enqueue_batch(log.name)


def _run_upload_batch(log, config):
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

	return len(rows) < batch_size


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
	file_url, content_hash = controller._compute_cloud_reference(
		backend, key, doc.is_private, doc.file_name, public_fallback_url=doc.file_url
	)
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


def _run_scan_batch(log, config):
	backend = controller.get_backend(config)
	if not backend:
		_append_error(log, None, "Could not initialise a cloud backend")
		return True

	bucket_type = log.bucket_type or "private"
	if log.current_batch_number == 1:
		frappe.db.sql("DELETE FROM `tabCloud Storage Object Index` WHERE bucket_type=%s", (bucket_type,))

	page = backend.list_keys_page(
		bucket_type=bucket_type,
		continuation_token=log.continuation_token or None,
		page_size=log.batch_size or SCAN_BATCH_SIZE,
	)
	_bulk_insert_index(page["objects"], bucket_type, log.name)
	log.objects_indexed = (log.objects_indexed or 0) + len(page["objects"])
	log.continuation_token = page["continuation_token"] or ""
	return not page["is_truncated"]


def _bulk_insert_index(objects, bucket_type, scan_log_name):
	if not objects:
		return
	now = frappe.utils.now()
	user = frappe.session.user
	value_groups = []
	params = []
	for key, size, last_modified in objects:
		value_groups.append("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0)")
		params.extend(
			[
				frappe.generate_hash(length=10),
				bucket_type,
				key,
				os.path.basename(key),
				size,
				_to_db_datetime(last_modified),
				scan_log_name,
				now,
				now,
				user,
				user,
			]
		)
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- value_groups holds only literal "(%s, ...)" placeholder groups, one per row; actual values are bound via params below
		f"""
		INSERT INTO `tabCloud Storage Object Index`
			(name, bucket_type, object_key, basename, size, last_modified, scan_log,
			 creation, modified, owner, modified_by, docstatus, idx)
		VALUES {", ".join(value_groups)}
		""",
		params,
	)


def _to_db_datetime(value):
	if not value or not hasattr(value, "strftime"):
		return None
	return value.strftime("%Y-%m-%d %H:%M:%S")


def _run_link_files_batch(log, config):
	backend = controller.get_backend(config)
	if not backend:
		_append_error(log, None, "Could not initialise a cloud backend")
		return True

	batch_size = log.batch_size or BATCH_SIZE
	rows = frappe.db.sql(
		"""
		SELECT name, file_url, file_name, is_private, attached_to_doctype, attached_to_name
		FROM `tabFile`
		WHERE is_folder = 0
		  AND name > %s
		ORDER BY name ASC
		LIMIT %s
		""",
		(log.cursor or "", batch_size),
		as_dict=True,
	)

	for row in rows:
		log.cursor = row["name"]
		_resolve_and_link_file_row(log, row, backend)

	return len(rows) < batch_size


def _resolve_and_link_file_row(log, row, backend):
	file_url = row.get("file_url")
	if not file_url or controller._is_cloud_file_url(file_url):
		log.skipped_no_url_or_cloud = (log.skipped_no_url_or_cloud or 0) + 1
		return

	candidates = _resolve_candidates(file_url, "private" if row["is_private"] else "public")

	if not candidates:
		log.flagged_unresolved = (log.flagged_unresolved or 0) + 1
		_create_reconciliation_issue(log, "File", row["name"], None, file_url, "Unresolved", [])
		return

	if len(candidates) > 1:
		log.flagged_for_review = (log.flagged_for_review or 0) + 1
		_create_reconciliation_issue(log, "File", row["name"], None, file_url, "Ambiguous", candidates)
		return

	candidate = candidates[0]
	try:
		doc = frappe.get_doc("File", row["name"])
		bucket_type = candidate.get("bucket_type") or ("private" if row["is_private"] else "public")
		result = controller.link_existing_object(
			doc, candidate["key"], bucket_type, backend=backend, verify=False
		)
		if result == "linked":
			log.migrated = (log.migrated or 0) + 1
		else:
			log.flagged_unresolved = (log.flagged_unresolved or 0) + 1
			_create_reconciliation_issue(log, "File", row["name"], None, file_url, "Unresolved", candidates)
	except Exception as e:
		log.skipped_other = (log.skipped_other or 0) + 1
		_append_error(log, row["name"], str(e))
		frappe.log_error(
			title=f"MultiCloud Storage link: {row['name']}",
			message=frappe.get_traceback(),
		)


def _resolve_candidates(raw_reference, bucket_type=None):
	"""Shared resolution path for both Link Existing Objects modes: prefer the site's
	key_resolver hook (it understands this site's legacy reference shapes); fall back
	to a plain basename lookup against the Cloud Storage Object Index when no hook is
	configured.
	"""
	candidates = controller.resolve_via_hook(raw_reference)
	if candidates is None:
		basename = os.path.basename(unquote(raw_reference))
		candidates = controller.lookup_by_basename(basename, bucket_type)
	return candidates or []


def _run_link_attach_fields_batch(log, config):
	backend = controller.get_backend(config)
	if not backend:
		_append_error(log, None, "Could not initialise a cloud backend")
		return True

	targets = frappe.get_all(
		"Cloud Storage Attach Field Target",
		filters={"enabled": 1},
		fields=["name", "target_doctype", "target_fieldname", "bucket_type", "require_manual_review"],
		order_by="name asc",
	)
	if not targets:
		return True

	target_idx, record_cursor = _parse_attach_cursor(log.cursor)
	batch_size = log.batch_size or BATCH_SIZE
	processed = 0

	while processed < batch_size and target_idx < len(targets):
		target = targets[target_idx]
		fetch_n = batch_size - processed
		rows = _fetch_attach_target_rows(target, record_cursor, fetch_n)

		for row in rows:
			record_cursor = row["name"]
			processed += 1
			_resolve_and_link_attach_row(log, target, row, backend)

		if len(rows) < fetch_n:
			target_idx += 1
			record_cursor = ""

	log.cursor = f"{target_idx}|{record_cursor}"
	return target_idx >= len(targets)


def _parse_attach_cursor(cursor):
	if not cursor:
		return 0, ""
	try:
		idx_str, record_cursor = cursor.split("|", 1)
		return int(idx_str), record_cursor
	except ValueError:
		return 0, ""


def _fetch_attach_target_rows(target, record_cursor, limit):
	doctype = target["target_doctype"]
	fieldname = target["target_fieldname"]
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- doctype/fieldname are validated identifiers (Cloud Storage Attach Field Target.validate() confirms the field exists and is Attach/Attach Image), not user input; identifiers can't be bound as %s params
		f"""
		SELECT name, `{fieldname}` AS raw_value
		FROM `tab{doctype}`
		WHERE `{fieldname}` IS NOT NULL AND `{fieldname}` != ''
		  AND name > %s
		ORDER BY name ASC
		LIMIT %s
		""",
		(record_cursor or "", limit),
		as_dict=True,
	)


def _resolve_and_link_attach_row(log, target, row, backend):
	raw_value = row.get("raw_value")
	if not raw_value or controller._is_cloud_file_url(raw_value):
		log.skipped_no_url_or_cloud = (log.skipped_no_url_or_cloud or 0) + 1
		return

	candidates = _resolve_candidates(raw_value, target["bucket_type"])

	if not candidates:
		log.flagged_unresolved = (log.flagged_unresolved or 0) + 1
		_create_reconciliation_issue(
			log,
			target["target_doctype"],
			row["name"],
			target["target_fieldname"],
			raw_value,
			"Unresolved",
			[],
		)
		return

	if len(candidates) > 1:
		log.flagged_for_review = (log.flagged_for_review or 0) + 1
		_create_reconciliation_issue(
			log,
			target["target_doctype"],
			row["name"],
			target["target_fieldname"],
			raw_value,
			"Ambiguous",
			candidates,
		)
		return

	if target.get("require_manual_review"):
		log.flagged_for_review = (log.flagged_for_review or 0) + 1
		_create_reconciliation_issue(
			log,
			target["target_doctype"],
			row["name"],
			target["target_fieldname"],
			raw_value,
			"Manual Review Required",
			candidates,
		)
		return

	candidate = candidates[0]
	try:
		created = _create_file_for_attach_value(row["name"], target, candidate, backend)
		if created:
			log.migrated = (log.migrated or 0) + 1
		else:
			log.skipped_other = (log.skipped_other or 0) + 1
	except Exception as e:
		log.skipped_other = (log.skipped_other or 0) + 1
		_append_error(log, f"{target['target_doctype']}:{row['name']}", str(e))
		frappe.log_error(
			title=f"MultiCloud Storage link attach: {target['target_doctype']} {row['name']}",
			message=frappe.get_traceback(),
		)


def _create_file_for_attach_value(record_name, target, candidate, backend):
	"""Create a File record for an Attach / Attach Image field value that was populated
	by direct data import and never went through Frappe's normal upload flow (so it has
	no tabFile row at all), then point the parent field at the new File's URL.

	Written with a raw INSERT rather than frappe.get_doc(...).insert() so it never goes
	through Frappe core's File controller (content/duplicate-detection logic that has no
	concept of a bucket key already sitting behind a repurposed content_hash column) and
	never fires this app's own after_insert hook -- the same discipline the rest of this
	app already applies to file_url/content_hash (see file_upload_to_cloud /
	_migrate_one_file, both of which finish with a raw UPDATE for exactly this reason).
	"""
	doctype = target["target_doctype"]
	fieldname = target["target_fieldname"]
	bucket_type = candidate.get("bucket_type") or target["bucket_type"]
	is_private = bucket_type == "private"
	key = candidate["key"]
	file_name = os.path.basename(key)

	file_url, content_hash = controller._compute_cloud_reference(backend, key, is_private, file_name)
	if not file_url:
		return False

	name = frappe.generate_hash(length=10)
	now = frappe.utils.now()
	user = frappe.session.user
	frappe.db.sql(
		"""
		INSERT INTO `tabFile`
			(name, file_name, file_url, content_hash, is_private, is_folder, folder,
			 attached_to_doctype, attached_to_name, attached_to_field,
			 creation, modified, owner, modified_by, docstatus, idx)
		VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0)
		""",
		(
			name,
			file_name,
			file_url,
			content_hash,
			1 if is_private else 0,
			"Home/Attachments",
			doctype,
			record_name,
			fieldname,
			now,
			now,
			user,
			user,
		),
	)
	frappe.db.set_value(doctype, record_name, fieldname, file_url, update_modified=False)
	return True


def _build_action_message(reason, candidates):
	"""Human-readable instructions for whoever works this issue -- the whole point of
	the review queue is that a person makes the final call instead of the engine
	guessing, so the record needs to say what call is actually being asked for.
	"""
	if reason == "Unresolved":
		return (
			"No object in the bucket matched this reference. Locate the correct object "
			"key by hand (check the raw reference and the bucket's actual layout), enter "
			"it into Resolved Key, then set Status to Resolved."
		)
	if reason == "Ambiguous":
		keys = [c.get("key", "") for c in candidates]
		shown = ", ".join(keys[:10])
		if len(keys) > 10:
			shown += f", and {len(keys) - 10} more"
		return (
			f"{len(candidates)} objects in the bucket share this reference's basename: "
			f"{shown}. Pick the correct one, enter it into Resolved Key, then set Status "
			f"to Resolved."
		)
	if reason == "Manual Review Required":
		key = candidates[0].get("key", "") if candidates else ""
		return (
			f"Exactly one candidate was found ({key}), but this field is configured to "
			f"always require manual review before linking. Verify it's correct -- or find "
			f"the right key if it isn't -- enter it into Resolved Key, then set Status to "
			f"Resolved."
		)
	return "Review this issue, enter the correct object key into Resolved Key, then set Status to Resolved."


def _create_reconciliation_issue(
	log, source_doctype, source_name, source_field, raw_reference, reason, candidates
):
	frappe.get_doc(
		{
			"doctype": "Cloud Storage Reconciliation Issue",
			"migration_log": log.name,
			"source_doctype": source_doctype,
			"source_name": source_name,
			"source_field": source_field,
			"raw_reference": (raw_reference or "")[:2000],
			"reason": reason,
			"action_required": _build_action_message(reason, candidates),
			"candidate_keys": frappe.as_json([c.get("key") for c in candidates]) if candidates else "",
		}
	).insert(ignore_permissions=True)


def _append_error(log, file_name, message):
	errors = frappe.parse_json(log.errors) if log.errors else []
	errors.append({"file": file_name, "error": message})
	log.errors = frappe.as_json(errors[-MAX_STORED_ERRORS:])


def _finalize(log, status):
	log.status = status
	log.ended_on = frappe.utils.now_datetime()
	log.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep
	_publish_progress(log)


def _publish_progress(log):
	frappe.publish_realtime(
		"cloud_storage_migration_progress",
		{
			"name": log.name,
			"status": log.status,
			"migration_type": log.migration_type,
			"current_batch_number": log.current_batch_number,
			"migrated": log.migrated or 0,
			"skipped_no_url_or_cloud": log.skipped_no_url_or_cloud or 0,
			"skipped_not_local_url": log.skipped_not_local_url or 0,
			"skipped_file_not_found": log.skipped_file_not_found or 0,
			"skipped_excluded_extension": log.skipped_excluded_extension or 0,
			"skipped_other": log.skipped_other or 0,
			"objects_indexed": log.objects_indexed or 0,
			"flagged_for_review": log.flagged_for_review or 0,
			"flagged_unresolved": log.flagged_unresolved or 0,
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


_BATCH_HANDLERS = {
	"Upload Local Files": _run_upload_batch,
	"Scan Bucket Index": _run_scan_batch,
	"Link Existing Objects (Files)": _run_link_files_batch,
	"Link Existing Objects (Attach Fields)": _run_link_attach_fields_batch,
}
