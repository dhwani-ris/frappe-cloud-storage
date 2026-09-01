# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CloudStorageReconciliationIssue(Document):
	def validate(self):
		if self.status == "Resolved" and not self.resolved_key:
			frappe.throw(
				frappe._(
					"Resolved Key is required before this issue can be marked Resolved -- "
					"record which object key is actually correct before closing it out."
				)
			)
