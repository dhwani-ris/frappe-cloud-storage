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
				__("Upload all local files (/files/ and /private/files/) to cloud. Continue?"),
				() => {
					frappe.call({
						method: "multi_cloud_storage.controller.migrate_existing_files",
						freeze: true,
						callback(r) {
							if (!r.message) return;
							const m = r.message;
							const migrated = m.migrated ?? 0;
							const skipped = m.skipped ?? 0;
							const total = m.total ?? 0;
							frappe.show_alert({
								message:
									__("Migrated") +
									` ${migrated} ` +
									__("file(s). Skipped:") +
									` ${skipped}. ` +
									__("Total:") +
									` ${total}.`,
								indicator: "blue",
							});
							const details = [];
							if (m.skipped_not_local_url)
								details.push(__("Not local URL:") + " " + m.skipped_not_local_url);
							if (m.skipped_no_url_or_cloud)
								details.push(
									__("No URL or on cloud:") + " " + m.skipped_no_url_or_cloud
								);
							if (m.skipped_file_not_found)
								details.push(
									__("File not on disk:") + " " + m.skipped_file_not_found
								);
							if (m.skipped_other)
								details.push(__("Other / error:") + " " + m.skipped_other);
							if (details.length) {
								frappe.msgprint({
									title: __("Migration details"),
									message: details.join("<br>"),
									indicator: "blue",
								});
							}
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
						},
					});
				}
			);
		});
	},
});
