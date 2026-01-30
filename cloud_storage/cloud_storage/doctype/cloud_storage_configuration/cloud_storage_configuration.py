# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CloudStorageConfiguration(Document):
	def validate(self):
		if not self.enabled:
			return
		if self.storage_provider == "Amazon S3":
			if not (self.s3_private_bucket_name or "").strip():
				frappe.throw(frappe._("S3 Private Bucket Name is required"))
			if not (self.s3_public_bucket_name or "").strip():
				frappe.throw(frappe._("S3 Public Bucket Name is required"))
		elif self.storage_provider == "Google Cloud Storage":
			if not (self.gcs_private_bucket_name or "").strip():
				frappe.throw(frappe._("GCS Private Bucket Name is required"))
			if not (self.gcs_public_bucket_name or "").strip():
				frappe.throw(frappe._("GCS Public Bucket Name is required"))
			if not (self.gcs_credentials_json or "").strip():
				frappe.throw(frappe._("GCS Service Account JSON is required"))
