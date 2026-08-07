// Copyright (c) 2026, Bhushan Barbuddhe and contributors
// For license information, please see license.txt

const RUNNING_STATUSES = ["Queued", "In Progress", "Cancelling"];

frappe.ui.form.on("Cloud Storage Migration Log", {
	refresh(frm) {
		frm.disable_save();

		if (["Queued", "In Progress"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Cancel Migration"), () => {
				frappe.confirm(
					__("The migration will stop after the batch currently in progress finishes. Continue?"),
					() => {
						frappe.call({
							method: "multi_cloud_storage.migration.cancel_migration",
							args: { migration_log: frm.doc.name },
							freeze: true,
							callback() {
								frm.reload_doc();
							},
						});
					}
				);
			}).addClass("btn-danger");
		}

		if (RUNNING_STATUSES.includes(frm.doc.status)) {
			frm.dashboard.set_headline_alert(
				__("Migration is running (batch {0}) — this page updates live.", [
					frm.doc.current_batch_number || 0,
				]),
				"blue"
			);
		}

		frappe.realtime.off("cloud_storage_migration_progress");
		if (RUNNING_STATUSES.includes(frm.doc.status)) {
			frappe.realtime.on("cloud_storage_migration_progress", (data) => {
				if (data.name === frm.doc.name) {
					frm.reload_doc();
				}
			});
		}
	},
});
