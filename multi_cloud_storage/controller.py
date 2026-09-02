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

INLINE_EXTENSIONS = {
	".jpg",
	".jpeg",
	".png",
	".gif",
	".webp",
	".svg",
	".bmp",
	".ico",
	".pdf",
	".mp4",
	".webm",
	".mp3",
	".wav",
	".ogg",
}


def _default_as_attachment(file_name):
	if not file_name:
		return True
	return Path(file_name).suffix.lower() not in INLINE_EXTENSIONS


def _parse_as_attachment(explicit, file_name):
	if explicit is None or explicit == "":
		return _default_as_attachment(file_name)
	return str(explicit).strip().lower() in ("1", "true", "yes")


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


def _compute_cloud_reference(backend, key, is_private, file_name=None, public_fallback_url=None):
	"""Build the (file_url, content_hash) pair `tabFile` stores once `key` is the
	object's location in the cloud, whether it was just uploaded there or was already
	sitting in the bucket. `content_hash` (repurposed here from Frappe's content-hash
	field) records bucket type + key; `file_url` for a private file is never the raw
	signed URL -- it's a redirect through `generate_file` that mints a fresh signed URL
	on every request, since signed URLs expire (signed_url_expiry_time, default 300s)
	long before the DB row would ever be read again.
	"""
	prefix = CONTENT_HASH_PRIVATE if is_private else CONTENT_HASH_PUBLIC
	content_hash = prefix + key
	if is_private:
		file_url = f"/api/method/multi_cloud_storage.controller.generate_file?key={quote(content_hash)}&file_name={quote(file_name or '')}"
	else:
		file_url = backend.get_public_url(key) if hasattr(backend, "get_public_url") else public_fallback_url
	return file_url, content_hash


def link_existing_object(doc, key, bucket_type, config=None, backend=None, verify=True):
	"""Point an existing File record at an object that is ALREADY present in the
	configured bucket, without uploading anything or touching local disk. Used to
	backfill references after files were moved into the bucket out-of-band (e.g. a
	prior migration from a legacy system straight into S3).

	Returns "linked", "not_found" (the object doesn't exist in that bucket), or
	"no_backend" (cloud storage isn't configured / enabled).

	`verify` controls whether object_exists() (a live network call) confirms the key
	first. Bulk callers resolving through the Cloud Storage Object Index already know
	the key exists -- a fresh index was just built from a live bucket listing -- and
	must pass verify=False, since a per-row HEAD check is exactly the bottleneck a
	bulk reconciliation is designed to avoid (millions of rows x one HTTP round-trip
	each). Small-scale/manual callers (e.g. linking one file by hand with a
	human-typed key) should keep the default True as a safety net.
	"""
	config = config or get_config()
	backend = backend or get_backend(config)
	if not backend:
		return "no_backend"
	if verify and not backend.object_exists(key, bucket_type):
		return "not_found"
	file_url, content_hash = _compute_cloud_reference(backend, key, bucket_type == "private", doc.file_name)
	frappe.db.sql(
		"""UPDATE `tabFile` SET file_url=%s, folder=%s, old_parent=%s, content_hash=%s
		WHERE name=%s""",
		(file_url, "Home/Attachments", "Home/Attachments", content_hash, doc.name),
	)
	doc.file_url = file_url
	doc.content_hash = content_hash
	return "linked"


def lookup_by_basename(basename, bucket_type=None):
	"""Look up candidate objects in the Cloud Storage Object Index by basename -- the
	fast, network-free way to resolve a legacy reference once the bucket has been
	scanned. Returns a list of {"key", "bucket_type", "size", "last_modified"} dicts:
	0 candidates means unresolved, 1 means an unambiguous match, 2+ means ambiguous
	(multiple objects in the bucket share this filename).
	"""
	if not basename:
		return []
	filters = {"basename": basename}
	if bucket_type:
		filters["bucket_type"] = bucket_type
	rows = frappe.get_all(
		"Cloud Storage Object Index",
		filters=filters,
		fields=["object_key as key", "bucket_type", "size", "last_modified"],
		limit=50,
	)
	return [dict(row) for row in rows]


def derive_key_from_url(raw_url):
	"""Best-effort, provider-agnostic guess at the object key hiding inside a raw legacy
	URL: strips scheme + host. For a virtual-hosted-style URL (bucket in the hostname)
	what's left after that IS the key. For a path-style URL (bucket as the first path
	segment) the caller still needs to strip that segment itself -- this helper can't
	tell the two shapes apart without knowing the site's legacy bucket name(s). Treat
	this as a starting point for a site's key_resolver hook, not a full resolver: always
	confirm the guess (e.g. via lookup_by_basename or object_exists) before trusting it.
	Returns None if raw_url isn't a recognisable URL shape.
	"""
	if not raw_url or not isinstance(raw_url, str):
		return None
	match = re.match(r"^s3://[^/]+/(.+)$", raw_url)
	if match:
		return unquote(match.group(1))
	match = re.match(r"^https?://[^/]+/(.+)$", raw_url)
	if match:
		return unquote(match.group(1))
	return None


def resolve_via_hook(raw_reference):
	"""Call the site's configured key-resolver hook (multi_cloud_storage_key_resolver),
	if any, to turn a raw legacy reference into candidate objects. This is the read-side
	counterpart to the existing key_generator hook: generating a key for a brand new
	upload is site-specific, and so is figuring out what key an old, already-uploaded
	reference corresponds to -- neither belongs in this app's generic core.

	Returns None if no hook is configured (caller should fall back to
	lookup_by_basename on its own), otherwise whatever list of candidate dicts
	({"key", "bucket_type", ...}) the hook returns (possibly empty).
	"""
	hook_cmd = frappe.get_hooks("multi_cloud_storage_key_resolver")
	if not hook_cmd:
		return None
	try:
		return frappe.get_attr(hook_cmd[0])(raw_reference=raw_reference) or []
	except Exception:
		frappe.log_error(
			title="MultiCloud Storage key_resolver hook failed",
			message=f"raw_reference={raw_reference!r}\n{frappe.get_traceback()}",
		)
		return []


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
	file_url, content_hash = _compute_cloud_reference(
		backend, key, doc.is_private, doc.file_name, public_fallback_url=path
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
def generate_file(key: str | None = None, file_name: str | None = None, as_attachment: str | None = None):
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
	attachment = _parse_as_attachment(as_attachment, file_name)
	url = backend.get_url(parsed_key, file_name, bucket_type, as_attachment=attachment)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = url


def _guard_no_active_run():
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
	return migration


@frappe.whitelist()
def migrate_existing_files():
	config = get_config()
	if not config:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	migration = _guard_no_active_run()
	log_name = migration.start_migration(migration_type="Upload Local Files")
	return {"migration_log": log_name}


@frappe.whitelist()
def scan_bucket_index(bucket_type: str = "private"):
	"""Kick off a background scan that lists every object already in the given bucket
	and indexes it (by basename) into Cloud Storage Object Index. Run this once (per
	bucket type) before Link Existing Objects -- linking resolves purely against this
	index, so it never needs a live existence check per row.
	"""
	config = get_config()
	if not config:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	if bucket_type not in ("private", "public"):
		frappe.throw(frappe._("bucket_type must be 'private' or 'public'"))
	migration = _guard_no_active_run()
	log_name = migration.start_migration(migration_type="Scan Bucket Index", bucket_type=bucket_type)
	return {"migration_log": log_name}


@frappe.whitelist()
def link_existing_files():
	"""Kick off a background job that resolves every non-cloud File row against the
	Cloud Storage Object Index (via the site's key_resolver hook) and links whatever
	resolves unambiguously. Requires scan_bucket_index to have been run first.
	"""
	config = get_config()
	if not config:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	migration = _guard_no_active_run()
	log_name = migration.start_migration(migration_type="Link Existing Objects (Files)")
	return {"migration_log": log_name}


@frappe.whitelist()
def link_existing_attach_fields():
	"""Kick off a background job that walks every enabled Cloud Storage Attach Field
	Target, resolves each record's raw field value against the Cloud Storage Object
	Index, and for whatever resolves unambiguously creates a File doc + updates the
	parent field. Requires scan_bucket_index to have been run first.
	"""
	config = get_config()
	if not config:
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	if not frappe.db.exists("Cloud Storage Attach Field Target", {"enabled": 1}):
		frappe.throw(frappe._("No enabled Cloud Storage Attach Field Target rows are configured"))
	migration = _guard_no_active_run()
	log_name = migration.start_migration(migration_type="Link Existing Objects (Attach Fields)")
	return {"migration_log": log_name}


@frappe.whitelist()
def link_existing_file(file_name: str, key: str, bucket_type: str = "private"):
	"""Whitelisted single-file entry point for manual / admin use -- e.g. resolving one
	Cloud Storage Reconciliation Issue by hand from the Desk. Bulk linking should go
	through link_existing_files / link_existing_attach_fields instead, which resolve
	via the Cloud Storage Object Index rather than one live existence check per row.
	"""
	if bucket_type not in ("private", "public"):
		frappe.throw(frappe._("bucket_type must be 'private' or 'public'"))
	doc = frappe.get_doc("File", file_name)
	result = link_existing_object(doc, key, bucket_type)
	if result == "no_backend":
		frappe.throw(frappe._("MultiCloud Storage is not enabled"))
	if result == "not_found":
		frappe.throw(frappe._("Object {0} was not found in the {1} bucket").format(key, bucket_type))
	return {"file_url": doc.file_url, "content_hash": doc.content_hash}


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
