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

		const start_job = (method, args, confirm_message) => {
			frappe.confirm(confirm_message, () => {
				frappe.call({
					method,
					args,
					freeze: true,
					callback(r) {
						if (!r.message || !r.message.migration_log) return;
						frappe.show_alert({
							message: __("Started in the background."),
							indicator: "green",
						});
						frappe.set_route(
							"Form",
							"Cloud Storage Migration Log",
							r.message.migration_log
						);
					},
				});
			});
		};

		frm.add_custom_button(
			__("Scan Bucket Index"),
			() => {
				frappe.prompt(
					{
						fieldname: "bucket_type",
						fieldtype: "Select",
						label: __("Bucket Type"),
						options: "private\npublic",
						default: "private",
						reqd: 1,
					},
					(values) => {
						start_job(
							"multi_cloud_storage.controller.scan_bucket_index",
							{ bucket_type: values.bucket_type },
							__(
								"List every object already in the {0} bucket and index it for reconciliation. Safe to re-run -- replaces the previous index for this bucket type. Continue?",
								[values.bucket_type]
							)
						);
					},
					__("Scan Bucket Index"),
					__("Start Scan")
				);
			},
			__("Reconcile Existing Objects")
		);

		frm.add_custom_button(
			__("Link Existing Objects (Files)"),
			() => {
				start_job(
					"multi_cloud_storage.controller.link_existing_files",
					{},
					__(
						"Resolve every File whose reference isn't already on cloud against the Cloud Storage Object Index, and link whatever matches unambiguously. Run Scan Bucket Index first. Continue?"
					)
				);
			},
			__("Reconcile Existing Objects")
		);

		frm.add_custom_button(
			__("Link Existing Objects (Attach Fields)"),
			() => {
				start_job(
					"multi_cloud_storage.controller.link_existing_attach_fields",
					{},
					__(
						"Resolve every configured Cloud Storage Attach Field Target against the Cloud Storage Object Index, creating File records and updating the parent field for whatever matches unambiguously. Run Scan Bucket Index first. Continue?"
					)
				);
			},
			__("Reconcile Existing Objects")
		);

		frm.add_custom_button(__("View Migration Logs"), () => {
			frappe.set_route("List", "Cloud Storage Migration Log");
		});

		frm.add_custom_button(
			__("Object Index"),
			() => frappe.set_route("List", "Cloud Storage Object Index"),
			__("Reconcile Existing Objects")
		);
		frm.add_custom_button(
			__("Attach Field Targets"),
			() => frappe.set_route("List", "Cloud Storage Attach Field Target"),
			__("Reconcile Existing Objects")
		);
		frm.add_custom_button(
			__("Reconciliation Issues"),
			() =>
				frappe.set_route("List", "Cloud Storage Reconciliation Issue", { status: "Open" }),
			__("Reconcile Existing Objects")
		);
	},
});
