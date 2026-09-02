// Copyright (c) 2026, Bhushan Barbuddhe and contributors
// For license information, please see license.txt

const ATTACH_FIELDTYPES = ["Attach", "Attach Image"];

function refresh_target_fieldname_options(frm) {
	if (!frm.doc.target_doctype) {
		frm.set_df_property("target_fieldname", "options", []);
		frm.refresh_field("target_fieldname");
		return;
	}

	frappe.model.with_doctype(frm.doc.target_doctype, () => {
		const meta = frappe.get_meta(frm.doc.target_doctype);
		const options = (meta.fields || [])
			.filter((df) => ATTACH_FIELDTYPES.includes(df.fieldtype))
			.map((df) => df.fieldname);

		frm.set_df_property("target_fieldname", "options", options);

		if (frm.doc.target_fieldname && !options.includes(frm.doc.target_fieldname)) {
			frm.set_value("target_fieldname", "");
		}
		if (!options.length) {
			frappe.show_alert({
				message: __("{0} has no Attach / Attach Image fields.", [frm.doc.target_doctype]),
				indicator: "orange",
			});
		}

		frm.refresh_field("target_fieldname");
	});
}

frappe.ui.form.on("Cloud Storage Attach Field Target", {
	refresh(frm) {
		refresh_target_fieldname_options(frm);
	},
	target_doctype(frm) {
		refresh_target_fieldname_options(frm);
	},
});
