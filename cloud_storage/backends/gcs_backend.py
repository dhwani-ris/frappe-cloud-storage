# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import datetime
import json
import random
import string

import frappe
from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage
from google.oauth2 import service_account

from .base import CloudStorageBackend


class GCSBackend(CloudStorageBackend):
	def __init__(self, config):
		self.config = config
		self._client = None

	@property
	def client(self):
		if self._client is None:
			raw = frappe.db.get_single_value("Cloud Storage Configuration", "gcs_credentials_json")
			if not raw or not raw.strip():
				frappe.throw(frappe._("GCS Service Account JSON is required"))
			try:
				creds_json = frappe.utils.password.decrypt(raw)
			except Exception:
				creds_json = raw
			try:
				info = json.loads(creds_json)
			except json.JSONDecodeError:
				frappe.throw(frappe._("Invalid GCS credentials JSON"))
			credentials = service_account.Credentials.from_service_account_info(info)
			self._client = storage.Client(credentials=credentials)
		return self._client

	def _bucket(self, bucket_type):
		field = "gcs_public_bucket_name" if bucket_type == "public" else "gcs_private_bucket_name"
		name = frappe.db.get_single_value("Cloud Storage Configuration", field)
		if not name:
			frappe.throw(frappe._("GCS {0} bucket name is not set").format(bucket_type))
		return self.client.bucket(name)

	def _strip_special_chars(self, file_name):
		return "".join(c for c in file_name if c.isalnum() or c in "._- ").replace(" ", "_")

	def key_generator(self, file_name, parent_doctype, parent_name):
		hook_cmd = frappe.get_hooks("cloud_storage_key_generator")
		if hook_cmd:
			try:
				k = frappe.get_attr(hook_cmd[0])(
					file_name=file_name,
					parent_doctype=parent_doctype,
					parent_name=parent_name,
				)
				if k:
					return k.rstrip("/").lstrip("/")
			except Exception:
				pass
		file_name = self._strip_special_chars(file_name)
		key_suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
		today = datetime.datetime.now()
		prefix = f"{today:%Y/%m/%d}/{parent_doctype}"
		if self.config.get("folder_name"):
			prefix = f"{self.config.folder_name}/{prefix}"
		return f"{prefix}/{key_suffix}_{file_name}"

	def upload(self, file_path, key, content_type, is_private, file_name=None):
		bucket_type = "private" if is_private else "public"
		bucket = self._bucket(bucket_type)
		blob = bucket.blob(key)
		blob.upload_from_filename(file_path, content_type=content_type)
		return key

	def delete(self, key, bucket_type="private"):
		if not key:
			return
		delete_enabled = frappe.db.get_single_value("Cloud Storage Configuration", "delete_file_from_cloud")
		if not delete_enabled:
			return
		bucket_name = frappe.db.get_single_value(
			"Cloud Storage Configuration",
			"gcs_public_bucket_name" if bucket_type == "public" else "gcs_private_bucket_name",
		)
		if not bucket_name:
			return
		bucket = self.client.bucket(bucket_name)
		try:
			bucket.delete_blob(key)
		except gcs_exceptions.NotFound:
			pass
		except Exception as e:
			frappe.log_error(
				title="Cloud Storage GCS delete failed",
				message=f"key={key!r} bucket_type={bucket_type} bucket={bucket_name}\n{frappe.get_traceback()}",
			)
			frappe.throw(frappe._("Could not delete file from cloud: {0}").format(str(e)))

	def get_url(self, key, file_name=None, bucket_type="private"):
		bucket = self._bucket(bucket_type)
		blob = bucket.blob(key)
		expiry = datetime.timedelta(seconds=self.config.signed_url_expiry_time or 300)
		return blob.generate_signed_url(version="v4", expiration=expiry, method="GET")

	def get_public_url(self, key):
		blob = self._bucket("public").blob(key)
		return blob.public_url

	def test_connection(self):
		try:
			self._bucket("private").reload()
			self._bucket("public").reload()
			return True, None
		except Exception as e:
			return False, str(e)
