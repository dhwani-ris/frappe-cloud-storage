// Copyright (c) 2026, Bhushan Barbuddhe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Cloud Storage Configuration", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint({
					title: __("Save First"),
					message: __("Please save your changes before testing the connection."),
					indicator: "blue",
				});
				return;
			}
			frappe.call({
				method: "cloud_storage.controller.test_connection",
				freeze: true,
				callback(r) {
					if (r.message && r.message.success) {
						frappe.show_alert({
							message: __("Connection successful"),
							indicator: "green",
						});
					} else {
						frappe.msgprint({
							title: __("Connection Failed"),
							message: (r.message && r.message.message) || __("Unknown error"),
							indicator: "red",
						});
					}
				},
			});
		}).addClass("btn-primary");

		frm.add_custom_button(__("Migrate Existing Files"), () => {
			frappe.confirm(
				__(
					"This will upload all local files (/files/ and /private/files/) to cloud. Continue?"
				),
				() => {
					frappe.call({
						method: "cloud_storage.controller.migrate_existing_files",
						freeze: true,
						callback(r) {
							if (r.message) {
								const m = r.message;
								const msg = __(
									"Migrated {0} file(s). Skipped: {1}. Total File records: {2}."
								).format(m.migrated || 0, m.skipped || 0, m.total || 0);
								frappe.show_alert({ message: msg, indicator: "blue" });
								if (m.errors && m.errors.length) {
									frappe.msgprint({
										title: __("Some files failed"),
										message: m.errors
											.map((e) => `${e.file}: ${e.error}`)
											.join("<br>"),
										indicator: "orange",
									});
								}
								frm.reload_doc();
							}
						},
					});
				}
			);
		});
	},
});
