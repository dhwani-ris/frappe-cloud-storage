# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import frappe

DEFAULT_EXCLUDED_EXTENSIONS = ".cer, .pem, .key"


def execute():
	"""Backfill the excluded_file_extensions default for sites where this field's
	column was added by a schema migration, since `default` in the DocType JSON
	only applies to newly created documents -- not to a Single that already existed
	before the field did."""
	if frappe.db.get_single_value("Cloud Storage Configuration", "excluded_file_extensions"):
		return
	frappe.db.set_single_value(
		"Cloud Storage Configuration", "excluded_file_extensions", DEFAULT_EXCLUDED_EXTENSIONS
	)
