# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import importlib
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

import frappe

BACKEND_IMPORTS = {
	"Amazon S3": (".backends.s3_backend", "S3Backend", "boto3"),
	"Google Cloud Storage": (".backends.gcs_backend", "GCSBackend", "google-cloud-storage"),
	"Azure Blob Storage": (".backends.azure_backend", "AzureBackend", "azure-storage-blob azure-identity"),
}


def get_config():
	config = frappe.get_single("Cloud Storage Configuration")
	if not config.enabled:
		return None
	return config


def get_backend(config=None):
	config = config or get_config()
	if not config:
		return None
	backend_info = BACKEND_IMPORTS.get(config.storage_provider)
	if not backend_info:
		return None
	module_path, class_name, pip_packages = backend_info
	try:
		module = importlib.import_module(module_path, package=__package__)
		backend_class = getattr(module, class_name)
	except ImportError as e:
		frappe.throw(
			frappe._(
				"The {0} backend could not be loaded because a required package is missing. "
				"Install it with: bench pip install {1} ({2})"
			).format(config.storage_provider, pip_packages, str(e))
		)
	return backend_class(config)


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
		r"^https?://[^.]+\.blob\.core\.windows\.net/",
	]
	return any(re.match(p, file_url) for p in patterns)


def _is_local_file_url(file_url):
	if not file_url or not isinstance(file_url, str):
		return False
	return file_url.startswith("/files/") or file_url.startswith("/private/files/")


def _parse_excluded_extensions(config):
	raw = (config.get("excluded_file_extensions") or "") if config else ""
	extensions = set()
	for part in raw.replace("\n", ",").split(","):
		ext = part.strip().lower()
		if not ext:
			continue
		if not ext.startswith("."):
			ext = "." + ext
		extensions.add(ext)
	return extensions


def _is_excluded_extension(file_name, config):
	extensions = _parse_excluded_extensions(config)
	if not extensions or not file_name:
		return False
	return Path(file_name).suffix.lower() in extensions


CONTENT_HASH_PRIVATE = "private:"
CONTENT_HASH_PUBLIC = "public:"


def _decode_query_param(value):
	"""Fully decode query string values (handles private%253A / private%3A → private:)."""
	if not value or not isinstance(value, str):
		return value
	s = value.strip()
	for _ in range(12):
		n = unquote(s)
		if n == s:
			break
		s = n
	return s


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
	config = get_config()
	if not config:
		return
	if _is_excluded_extension(doc.file_name, config):
		return
	backend = get_backend(config)
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
def generate_file(key: str | None = None, file_name: str | None = None):
	if not key:
		frappe.local.response["body"] = "Key not found."
		return
	key = _decode_query_param(key)
	if file_name:
		file_name = _decode_query_param(file_name)
	backend = get_backend()
	if not backend:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	parsed_key, bucket_type = _parse_content_hash(key)
	url = backend.get_url(parsed_key, file_name, bucket_type)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = url


@frappe.whitelist()
def migrate_existing_files():
	config = get_config()
	if not config:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	from . import migration

	active = frappe.db.exists(
		"Cloud Storage Migration Log",
		{"status": ["in", migration.ACTIVE_STATUSES]},
	)
	if active:
		frappe.throw(
			frappe._("A migration is already {0}. Open it to view progress: {1}").format(
				frappe.db.get_value("Cloud Storage Migration Log", active, "status"),
				frappe.utils.get_link_to_form("Cloud Storage Migration Log", active),
			)
		)
	log_name = migration.start_migration()
	return {"migration_log": log_name}


@frappe.whitelist()
def test_connection():
	config = get_config()
	if not config:
		return {
			"success": False,
			"message": frappe._("MultiCloud Storage is not enabled"),
		}
	backend = get_backend(config)
	if not backend:
		return {"success": False, "message": frappe._("Invalid provider configuration")}
	ok, err = backend.test_connection()
	if ok:
		return {"success": True, "message": frappe._("Connection successful")}
	return {"success": False, "message": err or frappe._("Connection failed")}
