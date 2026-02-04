# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import os
import re
from urllib.parse import quote

import frappe

from .backends.gcs_backend import GCSBackend
from .backends.s3_backend import S3Backend


def get_config():
	config = frappe.get_single("Cloud Storage Configuration")
	if not config.enabled:
		return None
	return config


def get_backend(config=None):
	config = config or get_config()
	if not config:
		return None
	if config.storage_provider == "Amazon S3":
		return S3Backend(config)
	if config.storage_provider == "Google Cloud Storage":
		return GCSBackend(config)
	return None


def _get_content_type(file_path):
	try:
		import magic

		return magic.from_file(file_path, mime=True)
	except Exception:
		return "application/octet-stream"


def _is_cloud_file_url(file_url):
	if not file_url:
		return False
	patterns = [
		r"^https?://.*\.s3\.amazonaws\.com/",
		r"^/api/method/multi_cloud_storage\.controller\.generate_file",
		r"^https://storage\.googleapis\.com/",
		r"^https://storage\.cloud\.google\.com/",
	]
	return any(re.match(p, file_url) for p in patterns)


def _is_local_file_url(file_url):
	if not file_url or not isinstance(file_url, str):
		return False
	return file_url.startswith("/files/") or file_url.startswith("/private/files/")


CONTENT_HASH_PRIVATE = "private:"
CONTENT_HASH_PUBLIC = "public:"


def _parse_content_hash(content_hash):
	if not content_hash or not isinstance(content_hash, str):
		return None, "private"
	s = content_hash.strip()
	if s.startswith(CONTENT_HASH_PRIVATE):
		return s[len(CONTENT_HASH_PRIVATE) :].strip(), "private"
	if s.startswith(CONTENT_HASH_PUBLIC):
		return s[len(CONTENT_HASH_PUBLIC) :].strip(), "public"
	return s.strip(), "private"


def file_upload_to_cloud(doc, method=None):
	if doc.attached_to_doctype == "Prepared Report":
		return
	backend = get_backend()
	if not backend:
		return
	ignore_doctypes = frappe.local.conf.get("ignore_multi_cloud_storage_doctype") or ["Data Import"]
	if doc.attached_to_doctype in ignore_doctypes:
		return
	site_path = frappe.utils.get_site_path()
	path = doc.file_url
	if not path or _is_cloud_file_url(path):
		return
	if doc.is_private:
		file_path = os.path.join(site_path, path.lstrip("/"))
	else:
		file_path = os.path.join(site_path, "public", path.lstrip("/"))
	if not os.path.isfile(file_path):
		return
	parent_doctype = doc.attached_to_doctype or "File"
	parent_name = doc.attached_to_name or ""
	if hasattr(backend, "key_generator"):
		key = backend.key_generator(doc.file_name, parent_doctype, parent_name)
	else:
		key = f"{parent_doctype}/{doc.file_name}"
	content_type = _get_content_type(file_path)
	backend.upload(file_path, key, content_type, doc.is_private, doc.file_name)
	prefix = CONTENT_HASH_PRIVATE if doc.is_private else CONTENT_HASH_PUBLIC
	content_hash = prefix + key
	if doc.is_private:
		file_url = f"/api/method/multi_cloud_storage.controller.generate_file?key={quote(content_hash)}&file_name={quote(doc.file_name or '')}"
	else:
		file_url = backend.get_public_url(key) if hasattr(backend, "get_public_url") else path
	try:
		os.remove(file_path)
	except OSError:
		pass
	frappe.db.sql(
		"""UPDATE `tabFile` SET file_url=%s, folder=%s, old_parent=%s, content_hash=%s
		WHERE name=%s""",
		(file_url, "Home/Attachments", "Home/Attachments", content_hash, doc.name),
	)
	doc.file_url = file_url
	doc.content_hash = content_hash


def delete_from_cloud(doc, method=None):
	backend = get_backend()
	if not backend or not doc.content_hash:
		return
	key, bucket_type = _parse_content_hash(doc.content_hash)
	if not key:
		return
	backend.delete(key, bucket_type)


@frappe.whitelist()
def generate_file(key=None, file_name=None):
	if not key:
		frappe.local.response["body"] = "Key not found."
		return
	backend = get_backend()
	if not backend:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	parsed_key, bucket_type = _parse_content_hash(key)
	url = backend.get_url(parsed_key, file_name, bucket_type)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = url


def _upload_existing_file(file_doc):
	backend = get_backend()
	if not backend:
		return False
	doc = file_doc
	path = (doc.file_url or "").strip()
	if not _is_local_file_url(path):
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
	content_type = _get_content_type(file_path)
	backend.upload(file_path, key, content_type, doc.is_private, doc.file_name)
	prefix = CONTENT_HASH_PRIVATE if doc.is_private else CONTENT_HASH_PUBLIC
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
	frappe.db.commit()
	return True


@frappe.whitelist()
def migrate_existing_files():
	config = get_config()
	if not config:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	files = frappe.get_all(
		"File",
		filters={"is_folder": 0},
		fields=["name", "file_url", "file_name", "is_private", "attached_to_doctype", "attached_to_name"],
	)
	migrated = 0
	skipped_no_url_or_cloud = 0
	skipped_not_local = 0
	skipped_file_not_found = 0
	skipped_other = 0
	errors = []
	for f in files:
		file_url = f.get("file_url")
		if not file_url or _is_cloud_file_url(file_url):
			skipped_no_url_or_cloud += 1
			continue
		if not _is_local_file_url(file_url):
			skipped_not_local += 1
			continue
		try:
			doc = frappe.get_doc("File", f["name"])
			result = _upload_existing_file(doc)
			if result is True:
				migrated += 1
			elif result == "file_not_found":
				skipped_file_not_found += 1
			else:
				skipped_other += 1
		except Exception as e:
			skipped_other += 1
			errors.append({"file": f["name"], "error": str(e)})
			frappe.log_error(
				title=f"MultiCloud Storage migrate: {f.get('name')}",
				message=frappe.get_traceback(),
			)
	return {
		"migrated": migrated,
		"total": len(files),
		"skipped": skipped_no_url_or_cloud + skipped_not_local + skipped_file_not_found + skipped_other,
		"skipped_no_url_or_cloud": skipped_no_url_or_cloud,
		"skipped_not_local_url": skipped_not_local,
		"skipped_file_not_found": skipped_file_not_found,
		"skipped_other": skipped_other,
		"errors": errors[:10],
	}


@frappe.whitelist()
def test_connection():
	config = get_config()
	if not config:
		return {"success": False, "message": frappe._("MultiCloud Storage is not enabled")}
	backend = get_backend(config)
	if not backend:
		return {"success": False, "message": frappe._("Invalid provider configuration")}
	ok, err = backend.test_connection()
	if ok:
		return {"success": True, "message": frappe._("Connection successful")}
	return {"success": False, "message": err or frappe._("Connection failed")}
