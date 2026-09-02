# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

from abc import ABC, abstractmethod


class CloudStorageBackend(ABC):
	@abstractmethod
	def upload(self, file_path, key, content_type, is_private, file_name=None):
		pass

	@abstractmethod
	def delete(self, key, bucket_type="private"):
		pass

	@abstractmethod
	def get_url(self, key, file_name=None, bucket_type="private", as_attachment=False):
		"""Return a signed URL for `key`. `as_attachment` controls the response's
		Content-Disposition: True forces a download (e.g. a CSV export), False serves
		it inline for in-browser preview (e.g. an image or PDF viewer widget).
		"""
		pass

	@abstractmethod
	def object_exists(self, key, bucket_type="private"):
		"""Return True if `key` already exists in the bucket, without downloading it.

		For single-key, small-scale checks only (e.g. linking one file by hand). Bulk
		reconciliation must go through list_keys_page() + the Cloud Storage Object Index
		instead -- checking millions of keys one at a time here does not scale.
		"""
		pass

	@abstractmethod
	def list_keys_page(self, bucket_type="private", prefix=None, continuation_token=None, page_size=1000):
		"""Return one page of (key, size, last_modified) tuples for the bucket, plus the
		token to pass back in for the next page.

		Returns a dict: {"objects": [(key, size, last_modified), ...],
		"continuation_token": <opaque token or None>, "is_truncated": bool}
		"""
		pass

	@abstractmethod
	def test_connection(self):
		pass
