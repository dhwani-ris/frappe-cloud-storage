# MultiCloud Storage

Multi-cloud file storage app for the Frappe framework. Uploads Frappe **File** attachments to **Amazon S3**, **Google Cloud Storage (GCS)**, or **Azure Blob Storage** and serves them from the cloud.

## Features

- **Three providers**: Amazon S3, Google Cloud Storage, and Azure Blob Storage; switch via single configuration.
- **Enable/disable**: All upload, delete, and migrate behaviour runs only when **Cloud Storage Configuration** is enabled.
- **Automatic upload**: New File attachments (via Attach or image fields) are uploaded to the configured bucket/container; local file is removed and `file_url` is updated to the cloud URL.
- **Two buckets/containers**: Separate **private** and **public** buckets/containers. Private: no public ACL; all access via signed URL. Public: objects get public-read (S3), make_public (GCS), or Blob-level public access (Azure); direct URLs. Avoids permission errors when the bucket/container blocks public access.
- **Private files**: Uploaded to the private bucket/container; served via time-limited signed URLs only.
- **Public files**: Uploaded to the public bucket/container with public read; `file_url` is the direct public URL.
- **Delete from cloud**: Optional "Delete file from cloud when File is deleted"; when enabled, deleting a File document also deletes the object from the bucket/container.
- **Test connection**: Toolbar button on Cloud Storage Configuration to verify bucket/container access.
- **Migrate existing files**: Toolbar button to upload all existing local File records to the configured cloud (skips files already on cloud).
- **Reconcile existing bucket contents**: For files that already sit in the bucket (moved there outside this app -- e.g. a prior migration from another system) but whose DB reference was never updated. Scans the bucket into a local index, then links File rows and/or Attach / Attach Image field values against it -- without re-uploading anything. See [Reconciling existing bucket contents](#reconciling-existing-bucket-contents) below.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO
bench install-app multi_cloud_storage
```

Install Python dependencies (if not installed by bench):

```bash
pip install -r apps/multi_cloud_storage/requirements.txt
```

Dependencies: `boto3`, `google-cloud-storage`, `azure-storage-blob`, `azure-identity`, `python-magic`.

## Configuration

Go to **Cloud Storage Configuration**.

| Field | Description |
|-------|-------------|
| **Enabled** | Turn cloud storage on/off. When off, no upload/delete/migrate runs. |
| **Delete file from cloud when File is deleted** | If enabled, deleting a File document also deletes the object in the bucket/container. |
| **Storage Provider** | `Amazon S3`, `Google Cloud Storage`, or `Azure Blob Storage`. |
| **Signed URL Expiry (seconds)** | Expiry for private-file signed URLs (default 300). |
| **Folder Prefix** | Optional prefix for object keys (e.g. `frappe-files`). |

### Amazon S3

| Field | Description |
|-------|-------------|
| Private Bucket Name | Bucket for private files (required). No public ACL; use signed URLs only. |
| Public Bucket Name | Bucket for public files (required). Objects get public-read ACL. |
| Region | AWS region (e.g. `us-east-1`). |
| Access Key ID | Optional; omit to use IAM role or env credentials. |
| Secret Access Key | Optional; required if Access Key ID is set. |

### Google Cloud Storage

| Field | Description |
|-------|-------------|
| Private Bucket Name | Bucket for private files (required). No make_public; signed URLs only. |
| Public Bucket Name | Bucket for public files (required). Objects get make_public. |
| Service Account JSON | Full JSON key for a service account with access to both buckets. |

### Azure Blob Storage

| Field | Description |
|-------|-------------|
| Storage Account Name | Azure storage account name (required). Used to construct all blob URLs. |
| Private Container Name | Container for private files (required). Must have **no** public access. |
| Public Container Name | Container for public files (required). Must have **Blob**-level public access enabled in the Azure portal. |
| Storage Account Key | Optional; leave blank to use Managed Identity (`azure-identity` must be installed). |

**Managed Identity:** When Storage Account Key is left blank, the backend uses `DefaultAzureCredential` from `azure-identity` (supports Managed Identity, environment variables, Azure CLI, etc.). Signed URLs in this mode use a User Delegation Key — the identity must have the **Storage Blob Delegator** role on the storage account.

You can use the same bucket/container for both by setting the same name for Private and Public; private files will still be served only via signed URL (no public ACL). Use **Test Connection** after saving to confirm access.

## How it works

- **Upload**: On File `after_insert`, if cloud storage is enabled and the file is on disk, it is uploaded to the **private** or **public** bucket/container according to `is_private`. The File row is updated with the cloud `file_url` and `content_hash` (stored as `private:key` or `public:key` so delete/URL generation know which bucket/container to use). The local file is removed.
- **Private files**: Stored in the private bucket/container; `file_url` is `/api/method/multi_cloud_storage.controller.generate_file?key=...`, which redirects to a signed URL. That signed URL's `Content-Disposition` is `inline` for images/PDF/audio/video (browser preview) and `attachment` for everything else (forced download) by default, based on the file's extension; append `&as_attachment=1` or `&as_attachment=0` to a `file_url` to override the default for a specific link.
- **Public files**: Stored in the public bucket/container with public read; `file_url` is the direct public URL.
- **Delete**: On File `on_trash`, if "Delete file from cloud" is enabled, the object is deleted from the correct bucket/container (parsed from `content_hash`).
- **Migrate**: Same logic; each file is uploaded to the private or public bucket/container by its `is_private` flag.

Object keys use a path like `{folder_prefix}/{YYYY}/{MM}/{DD}/{doctype}/{random}_{filename}` (or custom key if a hook is used).

## Reconciling existing bucket contents

Use this when files are already sitting in the configured bucket -- moved there by some other process, e.g. a migration from a legacy system straight into S3 -- but the DB (`tabFile.file_url`, or a raw Attach / Attach Image field value) still points somewhere else. This never uploads anything; it only verifies an object exists and writes the same `file_url`/`content_hash` pair the normal upload path would have written.

**1. Scan the bucket.** Cloud Storage Configuration → **Scan Bucket Index** (pick private or public). Lists every object in that bucket via the provider's paginated list API and indexes it by basename into **Cloud Storage Object Index**. Resumable if interrupted (stores a continuation token); a fresh scan replaces the previous index for that bucket type. Run once per bucket type before linking -- this is a bucket-size-bound listing step (a handful of API calls per 1,000 objects), independent of how many DB rows you're about to reconcile, and linking never does a live existence check per row because of it.

**2. (Attach fields only) Configure targets.** If you also need to fix Attach / Attach Image field values that were populated by direct data import and have no `tabFile` row at all, add rows to **Cloud Storage Attach Field Target**: DocType, fieldname, bucket type, and whether it requires manual review (turn this on for sensitive documents -- ID proofs, bank passbooks -- so a single clean match still goes to the review queue instead of auto-linking). Frappe's Data Import tool can bulk-load this list.

**3. Link.** Cloud Storage Configuration → **Link Existing Objects (Files)** and/or **Link Existing Objects (Attach Fields)**. Each row's raw reference is resolved to a candidate key -- via your site's `key_resolver` hook if configured, otherwise by basename lookup against the index -- and:
  - exactly one candidate, not flagged for manual review → linked automatically;
  - zero candidates, more than one candidate, or `require_manual_review` → routed to **Cloud Storage Reconciliation Issue** instead of guessed at.

**4. Review.** Work through open **Cloud Storage Reconciliation Issue** records, fill in `Resolved Key`, then either re-run the linking job (already-linked rows are skipped, so it's safe to run again) or link the single file by hand via `multi_cloud_storage.controller.link_existing_file`.

## Customisation

- **Ignore doctypes**: In `site_config.json` or environment, set `ignore_multi_cloud_storage_doctype` to a list of doctypes whose attachments should not be uploaded (e.g. `["Data Import", "Prepared Report"]`). "Prepared Report" is always ignored.
- **Custom key generator**: In your app's `hooks.py`, set `multi_cloud_storage_key_generator = ["your_app.utils.your_key_function"]`. The function receives `file_name`, `parent_doctype`, `parent_name` and should return the object key (string). Used when uploading a *new* file.
- **Custom key resolver**: In your app's `hooks.py`, set `multi_cloud_storage_key_resolver = ["your_app.utils.your_resolver_function"]`. The function receives `raw_reference` (the legacy string found in `file_url` or an Attach field) and should return a list of candidate dicts `{"key": ..., "bucket_type": "private"|"public"}` (empty list if it can't even form a guess). This is the read-side counterpart to the key generator: used when reconciling an *existing* reference against the bucket, in step 3 above. If no hook is configured, resolution falls back to a plain basename lookup against the Cloud Storage Object Index built in step 1.

## Contributing

Pre-commit is used for formatting and linting:

```bash
cd apps/multi_cloud_storage
pre-commit install
```

Tools: ruff, eslint, prettier, pyupgrade.

CI (GitHub Actions): installs the app and runs tests on push to `develop`; runs Semgrep and pip-audit on pull requests.

## License

MIT
