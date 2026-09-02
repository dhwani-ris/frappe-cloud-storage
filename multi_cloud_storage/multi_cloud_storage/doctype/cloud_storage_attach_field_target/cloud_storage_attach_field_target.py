# Copyright (c) 2026, Bhushan Barbuddhe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CloudStorageAttachFieldTarget(Document):
	def validate(self):
		meta = frappe.get_meta(self.target_doctype)
		field = meta.get_field(self.target_fieldname)
		if not field:
			frappe.throw(frappe._("{0} has no field {1}").format(self.target_doctype, self.target_fieldname))
		if field.fieldtype not in ("Attach", "Attach Image"):
			frappe.throw(
				frappe._("{0}.{1} is a {2} field, not Attach / Attach Image").format(
					self.target_doctype, self.target_fieldname, field.fieldtype
				)
			)
