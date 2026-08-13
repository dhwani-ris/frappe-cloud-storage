# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import datetime
import random
import string
from pathlib import Path

import frappe
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, ContentSettings, generate_blob_sas

from .base import CloudStorageBackend


class AzureBackend(CloudStorageBackend):
	def __init__(self, config):
		self.config = config
		self._client = None
		self._account_key = None

	@property
	def client(self):
		if self._client is None:
			account_name = self.config.get("azure_account_name")
			if not account_name:
				frappe.throw(frappe._("Azure Storage Account Name is required"))
			raw = frappe.db.get_single_value("Cloud Storage Configuration", "azure_account_key")
			credential = None
			if raw:
				try:
					self._account_key = frappe.utils.password.decrypt(raw)
				except Exception:
					self._account_key = raw
				credential = self._account_key
			else:
				try:
					from azure.identity import DefaultAzureCredential

					credential = DefaultAzureCredential()
				except ImportError:
					frappe.throw(
						frappe._(
							"azure-identity is required for Managed Identity auth. "
							"Provide an Account Key or install azure-identity."
						)
					)
			account_url = f"https://{account_name}.blob.core.windows.net"
			self._client = BlobServiceClient(account_url=account_url, credential=credential)
		return self._client

	def _container(self, bucket_type):
		if bucket_type == "public":
			return self.config.get("azure_public_container_name")
		return self.config.get("azure_private_container_name")

	def _strip_special_chars(self, file_name):
		return "".join(c for c in file_name if c.isalnum() or c in "._- ").replace(" ", "_")

	def key_generator(self, file_name, parent_doctype, parent_name):
		hook_cmd = frappe.get_hooks("multi_cloud_storage_key_generator")
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
		container_name = self._container("private" if is_private else "public")
		blob_client = self.client.get_blob_client(container=container_name, blob=key)
		try:
			blob_client.upload_blob(
				Path(file_path).read_bytes(),
				content_settings=ContentSettings(content_type=content_type),
				metadata={"file_name": file_name or ""},
				overwrite=True,
			)
		except Exception as e:
			frappe.throw(frappe._("File upload failed: {0}").format(str(e)))
		return key

	def delete(self, key, bucket_type="private"):
		if not key:
			return
		delete_enabled = frappe.db.get_single_value("Cloud Storage Configuration", "delete_file_from_cloud")
		if not delete_enabled:
			return
		container_name = self._container(bucket_type)
		if not container_name:
			return
		blob_client = self.client.get_blob_client(container=container_name, blob=key)
		try:
			blob_client.delete_blob()
		except ResourceNotFoundError:
			pass
		except Exception as e:
			frappe.log_error(
				title="MultiCloud Storage Azure delete failed",
				message=f"key={key!r} bucket_type={bucket_type} container={container_name}\n{frappe.get_traceback()}",
			)
			frappe.throw(frappe._("Could not delete file from cloud: {0}").format(str(e)))

	def get_url(self, key, file_name=None, bucket_type="private"):
		account_name = self.config.get("azure_account_name")
		container_name = self._container(bucket_type)
		expiry = self.config.signed_url_expiry_time or 300
		_ = self.client  # ensure client is initialised and _account_key is populated
		expiry_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=expiry)
		if self._account_key:
			sas_token = generate_blob_sas(
				account_name=account_name,
				container_name=container_name,
				blob_name=key,
				account_key=self._account_key,
				permission=BlobSasPermissions(read=True),
				expiry=expiry_time,
			)
		else:
			user_delegation_key = self.client.get_user_delegation_key(
				key_start_time=datetime.datetime.utcnow(),
				key_expiry_time=expiry_time,
			)
			sas_token = generate_blob_sas(
				account_name=account_name,
				container_name=container_name,
				blob_name=key,
				user_delegation_key=user_delegation_key,
				permission=BlobSasPermissions(read=True),
				expiry=expiry_time,
			)
		return f"https://{account_name}.blob.core.windows.net/{container_name}/{key}?{sas_token}"

	def get_public_url(self, key):
		account_name = self.config.get("azure_account_name")
		container_name = self._container("public")
		return f"https://{account_name}.blob.core.windows.net/{container_name}/{key}"

	def test_connection(self):
		try:
			self.client.get_container_client(self._container("private")).get_container_properties()
			self.client.get_container_client(self._container("public")).get_container_properties()
			return True, None
		except Exception as e:
			return False, str(e)
