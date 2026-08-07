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
				method: "multi_cloud_storage.controller.test_connection",
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
					"Upload all local files (/files/ and /private/files/) to cloud in the background. Continue?"
				),
				() => {
					frappe.call({
						method: "multi_cloud_storage.controller.migrate_existing_files",
						freeze: true,
						callback(r) {
							if (!r.message || !r.message.migration_log) return;
							frappe.show_alert({
								message: __("Migration started in the background."),
								indicator: "green",
							});
							frappe.set_route(
								"Form",
								"Cloud Storage Migration Log",
								r.message.migration_log
							);
						},
					});
				}
			);
		});

		frm.add_custom_button(__("View Migration Logs"), () => {
			frappe.set_route("List", "Cloud Storage Migration Log");
		});
	},
});
