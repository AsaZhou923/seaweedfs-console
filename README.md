# SeaweedFS Console

**A third-party web console for SeaweedFS cluster management and image asset workflows.**

English | [简体中文](README.zh-CN.md)

Manage clusters, buckets, IAM, files, and background services through one interface, with an image workbench for browsing, previews, diagnostics, duplicate detection, and derived variants.

SeaweedFS Console is an independent project, not the official SeaweedFS Admin UI. Its management adapters target a pinned **SeaweedFS OSS 4.48** baseline. Capabilities depend on the services and permissions available in your deployment.

## Features

| Area | Capabilities |
| --- | --- |
| Cluster | Dashboard, topology, configured service health, volumes, erasure coding, and collections |
| Buckets | Creation and guarded deletion, quotas, ownership, lifecycle rules, policies, versioning, and object lock |
| IAM | Users, access keys, groups, service accounts, and policies |
| Files and objects | Filer browsing and file operations; scoped S3 browsing, uploads, copies, downloads, and guarded version deletion |
| Workers and modules | Worker/plugin status, jobs, maintenance actions, MQ management, configured Mount clients, and S3 Tables/Iceberg/Lance previews |
| Image workbench | Indexed browsing and filters, bounded previews, metadata and access diagnostics, SHA256 duplicate candidates, presets, derived images, and capacity reports |

- **English and Simplified Chinese** interfaces, with English as the default.
- **Local administrator login** with server-side sessions, CSRF protection, and action/Scope checks.
- **Backend-only credentials**, restricted to explicitly approved endpoints.
- **Guarded writes** with durable intents, stable idempotency keys, and readback verification.
- **Persistent jobs** handled by a standalone worker and SQLite WAL.

## Quick start

The maintained local setup uses **Windows, PowerShell 7.5, Python 3.12, and Node.js 24**. You also need access to a SeaweedFS deployment; this project does not start SeaweedFS itself.

From the project root:

### 1. Configure credentials

```powershell
if (-not (Test-Path .\secrets.json)) {
    Copy-Item .\secrets.example.json .\secrets.json
}
```

Edit `secrets.json` and replace the example credentials and endpoints with your own. See [secrets.example.json](secrets.example.json) for S3, Admin, native HTTP, and optional gRPC configuration.

S3 credentials require a nonempty `allowed_endpoint_url`. Admin credentials bind the Admin URL and explicitly list approved native service endpoints. Only configure endpoints you intend the backend to access.

### 2. Install and start

```powershell
$env:CONSOLE_ADMIN_PASSWORD = 'replace-with-a-strong-password'
$env:CONSOLE_SECRETS_FILE = Join-Path (Get-Location) 'secrets.json'
pwsh -NoProfile -File scripts/start.ps1 -Install
```

The script creates a Python virtual environment, installs locked dependencies, builds the frontend, and starts the API and worker in the background.

Open **http://127.0.0.1:18765** and sign in as `admin` with the password you configured. Logs and process records are written to `output/`.

The administrator password is stored as a hash on first startup. Changing the environment variable later does not reset an existing account.

### 3. Connect your services

1. Open **Settings** and create a Management Connection using your registered Admin credential reference.
2. Configure the approved Admin and native service endpoints. Associate an S3 connection where S3 functionality is needed.
3. For the image workbench, create a project and a Scope bound to a bucket and literal key prefix. Enable preview, original download, or writes only as needed.
4. Submit a scan to index objects. Submit a separate checksum scan to find exact duplicate candidates.

Management writes are disabled by default. Enable only the actions and target ranges you need. Missing dependencies and unsupported capabilities are reported explicitly.

### Stop or restart

```powershell
pwsh -NoProfile -File scripts/stop.ps1
pwsh -NoProfile -File scripts/start.ps1
```

Keep your configuration environment variables set when restarting. API and worker processes can also be started separately from `backend/` with the same absolute data and credential paths.

## Configuration

[.env.example](.env.example) documents the available environment variables. The startup scripts **do not automatically load this file**.

| Variable | Purpose |
| --- | --- |
| `CONSOLE_ADMIN_USERNAME` | Administrator username; defaults to `admin` |
| `CONSOLE_ADMIN_PASSWORD` | Required startup password; initializes the account on first use |
| `CONSOLE_DATA_DIR` | SQLite database and private preview data; defaults to the project's `data/` with the startup script |
| `CONSOLE_SECRETS_FILE` | Backend credential registry path |
| `CONSOLE_ALLOWED_ORIGINS` | Comma-separated allowed browser origins |
| `CONSOLE_DEV_INSECURE_COOKIE` | `1` for local HTTP development; use `0` with HTTPS |
| `CONSOLE_CACHE_MAX_BYTES` | Private preview cache size limit |
| `CONSOLE_CACHE_TTL_SECONDS` | Private preview cache retention period |

Use absolute data and registry paths when starting processes from different directories. Never commit `secrets.json`, `.env`, database files, sessions, or generated credentials.

## How it works

```text
Browser → FastAPI API → approved SeaweedFS Admin / native / S3 endpoints
                   ↘ SQLite WAL ← standalone worker
```

The React application and API share an origin in the local setup. The backend owns upstream credentials and sessions; the browser talks to the console API. SQLite stores configuration, indexes, job state, manifests, and operation history. Source objects remain in SeaweedFS.

Built with **React, TypeScript, Vite, FastAPI, boto3, Pillow, and SQLite**. Optional module adapters use gRPC and bounded table decoders.

## Development

Dependency versions are recorded in [backend/requirements-lock.txt](backend/requirements-lock.txt) and [frontend/package-lock.json](frontend/package-lock.json).

After the first installation, you can run the frontend development server against the API on port `8000`:

```powershell
pwsh -NoProfile -File scripts/stop.ps1
$env:CONSOLE_ALLOWED_ORIGINS = 'http://127.0.0.1:5173,http://127.0.0.1:8000'
pwsh -NoProfile -File scripts/start.ps1 -Port 8000
npm run dev --prefix frontend
```

Use the same administrator password and credential environment variables as in the quick start. Open **http://127.0.0.1:5173**. Vite proxies `/api` to `http://127.0.0.1:8000`; restart the API/worker after backend changes.

### Checks

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=
.venv\Scripts\python.exe -m pytest tests/scripts -q -p no:cacheprovider -o addopts=
npm run build --prefix frontend
npm run test:i18n --prefix frontend
node scripts/verify_table_preview_ui.cjs
node scripts/verify_frontend_review_regressions.cjs
```

The frontend regression script requires a built `frontend/dist` and Chrome or Edge. It uses a fully mocked local service. Live integration scripts have real side effects; read them and confirm their target scope before running them.

### Local bundles

Build the frontend before creating a source-and-static bundle. Choose a fresh destination outside source directories and historical release directories:

```powershell
.venv\Scripts\python.exe scripts/package.py --destination output/local-build/console.zip
.venv\Scripts\python.exe scripts/verify_package.py --package output/local-build/console.zip --registry secrets.json
```

Package verification checks the archive, manifest, required files, relative documentation links, and credential values from the supplied and discovered registries. A build alone does not prove that credential bytes are absent.

## Backup and recovery

```powershell
.venv\Scripts\python.exe scripts/backup.py backup data/console.db output/backups/console.db
.venv\Scripts\python.exe scripts/backup.py verify output/backups/console.db
.venv\Scripts\python.exe scripts/backup.py restore output/backups/console.db output/restore/console.db
```

Backup and restore destinations must be new paths. The helper uses SQLite's backup API and verifies integrity and foreign keys. Stop the API and worker before activating a restored database, then set `CONSOLE_DATA_DIR` to the restored directory and restart. Back up credentials and environment configuration separately.

This backs up console state, not S3 objects or Filer metadata.

## Current scope

- Maintained for a single local administrator and the pinned SeaweedFS OSS baseline. Enterprise OIDC, multi-user roles, and multi-cluster HA are outside the current scope.
- Table previews are bounded samples, not complete logical queries. Lance samples require an available Worker; missing dependencies remain unknown.
- Unmeasured capacity and disconnected business references remain unknown. ETag is not a complete content hash. Automatic source-image deletion remains disabled without reference and concurrency protection.
- Large-scale workloads, arbitrary old-database upgrades, and production global-write behavior require separate validation. The full migration-audit ledger is still pending.
- The startup script binds the API to localhost. Network exposure requires separate HTTPS and origin configuration.

## Contributing

Bug reports and focused pull requests are welcome. Include the affected module, reproduction steps, your SeaweedFS version, and sanitized errors. Omit credentials, signed URLs, session data, and private object contents.

Keep changes scoped, add meaningful regression coverage for behavior changes, and update both language catalogs for owned UI text. See [AGENTS.md](AGENTS.md) for project conventions and the [management API reference](docs/management-api.md) for adapter contracts.

## License

Original project code is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution. Third-party components retain their respective licenses, including the [vendored SeaweedFS protocol license](backend/console/vendor/SEAWEEDFS-LICENSE.txt).
