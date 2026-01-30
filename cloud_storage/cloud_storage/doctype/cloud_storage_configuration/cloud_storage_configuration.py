# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

SECRET_PLACEHOLDER = "********"


def _is_placeholder(value):
	if not value or not value.strip():
		return True
	s = value.strip()
	return s == SECRET_PLACEHOLDER or (len(set(s)) == 1 and s[0] == "*")


class CloudStorageConfiguration(Document):
	def validate(self):
		if not self.enabled:
			return
		if self.storage_provider == "Amazon S3":
			if not (self.s3_private_bucket_name or "").strip():
				frappe.throw(frappe._("S3 Private Bucket Name is required"))
			if not (self.s3_public_bucket_name or "").strip():
				frappe.throw(frappe._("S3 Public Bucket Name is required"))
			self._validate_and_encrypt_s3_secret()
		elif self.storage_provider == "Google Cloud Storage":
			if not (self.gcs_private_bucket_name or "").strip():
				frappe.throw(frappe._("GCS Private Bucket Name is required"))
			if not (self.gcs_public_bucket_name or "").strip():
				frappe.throw(frappe._("GCS Public Bucket Name is required"))
			self._validate_and_encrypt_gcs_json()

	def _validate_and_encrypt_s3_secret(self):
		val = (self.s3_aws_secret or "").strip()
		if _is_placeholder(val):
			existing = frappe.db.get_value(self.doctype, self.name, "s3_aws_secret")
			if existing:
				self.s3_aws_secret = existing
		elif val:
			self.s3_aws_secret = frappe.utils.password.encrypt(val)

	def _validate_and_encrypt_gcs_json(self):
		val = (self.gcs_credentials_json or "").strip()
		if _is_placeholder(val):
			existing = frappe.db.get_value(self.doctype, self.name, "gcs_credentials_json")
			if existing:
				self.gcs_credentials_json = existing
			else:
				frappe.throw(frappe._("GCS Service Account JSON is required"))
		elif val.startswith("{"):
			self.gcs_credentials_json = frappe.utils.password.encrypt(val)

	def as_dict(self, *args, **kwargs):
		d = super().as_dict(*args, **kwargs)
		if d.get("s3_aws_secret"):
			d["s3_aws_secret"] = SECRET_PLACEHOLDER
		if d.get("gcs_credentials_json"):
			d["gcs_credentials_json"] = SECRET_PLACEHOLDER
		return d
