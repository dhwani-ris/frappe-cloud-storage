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
	def get_url(self, key, file_name=None, bucket_type="private"):
		pass

	@abstractmethod
	def test_connection(self):
		pass
