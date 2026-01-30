# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import datetime
import random
import re
import string

import boto3
import frappe
from botocore.client import Config
from botocore.exceptions import ClientError

from .base import CloudStorageBackend


class S3Backend(CloudStorageBackend):
	def __init__(self, config):
		self.config = config
		self._client = None

	@property
	def client(self):
		if self._client is None:
			kwargs = {
				"region_name": self.config.get("s3_region_name") or "us-east-1",
				"config": Config(signature_version="s3v4"),
			}
			aws_key = self.config.get("s3_aws_key")
			raw = frappe.db.get_single_value("Cloud Storage Configuration", "s3_aws_secret")
			aws_secret = None
			if raw:
				try:
					aws_secret = frappe.utils.password.decrypt(raw)
				except Exception:
					aws_secret = raw
			if aws_key and aws_secret:
				kwargs["aws_access_key_id"] = aws_key
				kwargs["aws_secret_access_key"] = aws_secret
			self._client = boto3.client("s3", **kwargs)
		return self._client

	def _bucket(self, bucket_type):
		if bucket_type == "public":
			return self.config.s3_public_bucket_name
		return self.config.s3_private_bucket_name

	def _strip_special_chars(self, file_name):
		return re.sub(r"[^0-9a-zA-Z._-]", "", file_name)

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
		file_name = file_name.replace(" ", "_")
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
		extra = {"ContentType": content_type, "Metadata": {"file_name": file_name or ""}}
		if not is_private:
			extra["ACL"] = "public-read"
		try:
			self.client.upload_file(file_path, bucket, key, ExtraArgs=extra)
		except Exception as e:
			frappe.throw(frappe._("File upload failed: {0}").format(str(e)))
		return key

	def delete(self, key, bucket_type="private"):
		if not self.config.delete_file_from_cloud:
			return
		bucket = self._bucket(bucket_type)
		try:
			self.client.delete_object(Bucket=bucket, Key=key)
		except ClientError:
			frappe.throw(frappe._("Could not delete file from cloud"))

	def get_url(self, key, file_name=None, bucket_type="private"):
		bucket = self._bucket(bucket_type)
		expiry = self.config.signed_url_expiry_time or 300
		params = {"Bucket": bucket, "Key": key}
		if file_name:
			params["ResponseContentDisposition"] = f"filename={file_name}"
		return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expiry)

	def get_public_url(self, key):
		bucket = self._bucket("public")
		endpoint = self.client.meta.endpoint_url
		return f"{endpoint}/{bucket}/{key}"

	def test_connection(self):
		try:
			self.client.head_bucket(Bucket=self._bucket("private"))
			self.client.head_bucket(Bucket=self._bucket("public"))
			return True, None
		except ClientError as e:
			return False, str(e)
