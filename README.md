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
- **Private files**: Stored in the private bucket/container; `file_url` is `/api/method/multi_cloud_storage.controller.generate_file?key=...`, which redirects to a signed URL.
- **Public files**: Stored in the public bucket/container with public read; `file_url` is the direct public URL.
- **Delete**: On File `on_trash`, if "Delete file from cloud" is enabled, the object is deleted from the correct bucket/container (parsed from `content_hash`).
- **Migrate**: Same logic; each file is uploaded to the private or public bucket/container by its `is_private` flag.

Object keys use a path like `{folder_prefix}/{YYYY}/{MM}/{DD}/{doctype}/{random}_{filename}` (or custom key if a hook is used).

## Customisation

- **Ignore doctypes**: In `site_config.json` or environment, set `ignore_multi_cloud_storage_doctype` to a list of doctypes whose attachments should not be uploaded (e.g. `["Data Import", "Prepared Report"]`). "Prepared Report" is always ignored.
- **Custom key generator**: In your app's `hooks.py`, set `multi_cloud_storage_key_generator = ["your_app.utils.your_key_function"]`. The function receives `file_name`, `parent_doctype`, `parent_name` and should return the object key (string).

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
