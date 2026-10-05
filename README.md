# SeaweedFS Console

Languages: English | [简体中文](README.zh-CN.md)

Third-party SeaweedFS OSS Admin enhancement console. This is not the official SeaweedFS Admin. The stack is React + TypeScript + Vite, FastAPI, SQLite WAL, and a standalone worker. The shared foundation and image asset enhancements are complete, and official admin core tasks 28-37 are complete.

UI language: English is the default. The English / 简体中文 selector is available before login and inside the app. The selection is persisted locally when storage is available. When storage is unavailable, switching still works for the current page and the next visit starts in English.

UI translations are maintained in `frontend/src/i18n/`. Add both English and Chinese entries when adding UI copy. Object keys, user-defined names, API identifiers, and raw API evidence retain their original values.

Development and acceptance are based on the [development plan](<E:/Project Code/docs/01 - Projects/seaweedfs-console/08 - Plans/seaweedfs-console 开发计划.md>). Tasks 01-22 are the completed shared foundation and image enhancements. The official OSS Admin management core is the added 28-37 line, currently 10/10 complete: 28, 29, 30, 31, 32, 33, 34, 35, 36, and 37 are complete. Tasks 23-27 remain reserved for later advanced topics. Progress and untested scope are tracked in the plan and [test fix index](docs/test-fix-log.md). The current official management API reference is [docs/management-api.md](docs/management-api.md).

## Local startup (currently maintained path)

Windows PowerShell 7.5, Python 3.12, Node 24. Installed versions are locked by `backend/requirements-lock.txt` and `frontend/package-lock.json`. The API listens only on localhost. Production network exposure and server deployment require separate HTTPS configuration.

Create `secrets.json` in the project root, using `secrets.example.json` as the structural reference. The file is read only by the backend; the connection table stores only `secret_ref`. A nonempty `allowed_endpoint_url` is mandatory before creating or using an S3 connection. `allowed_connection_id` can additionally be bound after the first connection is created. Do not commit real credentials or expose them to the browser.

```powershell
$env:CONSOLE_ADMIN_PASSWORD = '<set an independent admin password>'
$env:CONSOLE_SECRETS_FILE = 'E:\Project Code\my_repo\seaweedfs-console\secrets.json'
pwsh -NoProfile -File scripts/start.ps1 -Install
```

Open `http://127.0.0.1:18765`. The default account is `admin`. `start.ps1` starts the standalone API and worker; logs and PID files are written under `output/`. After the password hash is written for the first time, changing the environment variable does not reset the account stored in the database. `CONSOLE_DEV_INSECURE_COOKIE=1` is only for the local HTTP development path above; use `0` for HTTPS.

```powershell
pwsh -NoProfile -File scripts/stop.ps1
```

You can also start the API and worker from `backend/` in two terminals, using the same absolute data directory and the same configuration:

```powershell
..\.venv\Scripts\python.exe -m uvicorn console.main:create_app --factory --host 127.0.0.1 --port 18765
..\.venv\Scripts\python.exe -m console.jobs
```

`.env.example` is an environment variable reference. The startup scripts do not automatically execute it. Prefer absolute paths for all `CONSOLE_*` paths so the API and worker do not point to different databases.

## First connection

1. Register the Admin secret in `secrets.json` first: set `kind: "seaweed_admin"`, bind `allowed_endpoint_url` to the Admin UI, and list the allowed Master, Filer, and Volume management endpoints in `allowed_endpoints`. For S3 secrets, bind `allowed_endpoint_url` to the S3 endpoint. When S3Tables/DataPreview is needed, the management connection's `endpoints.s3` must match this approved S3 endpoint.
2. Create a Management Connection on the Settings page, bind the Admin secret, and fill in the Admin, Master, Filer, Volume, and optional S3 endpoints for the actual environment. Dashboard, Topology, Services, Volumes, Buckets, IAM, Workers, and Modules use the Management Connection first.
3. After Dashboard / Topology pass validation, continue to Buckets, Objects, Files, or Modules. Object browsing, indexing, and preview can use readonly scope. Derived writes and uploads require only the corresponding output scope to be writable. Only manual S3 physical deletion, version deletion, and conditional deletion require an associated S3 connection with writable / `object.manage` explicitly enabled.
4. The image asset workbench is an enhancement module. Create a logical project and enter a known bucket and literal prefix. It can be connected without ListBuckets permission; preview, source image download, and target writes must be explicitly enabled.
5. Submit a scan and review real processed counts, failed items, pause/cancel state, and resume state on the jobs page. Unknown totals do not show a percentage. Jobs remain queued while the worker is offline.
6. Submit a checksum deep scan when exact duplicate candidates are required. ETag is not SHA256, and unscanned objects do not mean zero duplicates. Derived assets are written to new keys and source images are retained. Move and automatic source-image deletion remain disabled without a concurrent reference protection protocol.

## Persistent data and recovery

The data directory contains `console.db`, WAL/SHM files, and private `previews/`. Connections, assets, jobs, specs, derived manifests, and operation records are stored in SQLite. Thumbnails can be regenerated. Do not copy only a running `.db` file.

```powershell
.venv\Scripts\python.exe scripts/backup.py backup data/console.db output/backups/console-20261004.db
.venv\Scripts\python.exe scripts/backup.py verify output/backups/console-20261004.db
.venv\Scripts\python.exe scripts/backup.py restore output/backups/console-20261004.db output/restore/console.db
```

Backup uses the SQLite backup API, verifies integrity/FK, and generates table counts plus a SHA256 manifest. The restore target must not already exist. After verification, stop the API/worker first, then start with a newly configured data directory. Credentials and environment configuration must be backed up separately and securely. This feature backs up only console data; S3 objects and Filer metadata disaster recovery belong to later task 25.

## Management connection configuration

Management integration requires endpoint-bound credentials in `secrets.json`. `secrets.example.json` contains both shapes: S3 credentials use `allowed_endpoint_url` bound to the S3 endpoint, while Admin credentials use `kind: "seaweed_admin"`, `allowed_endpoint_url` bound to the Admin UI, and `allowed_endpoints` listing master/filer/volume/s3. When `scripts/fetch_test_credentials.py` fetches OptiPlex test credentials, it generates `server-admin` and a test S3 secret. S3Tables/DataPreview reads require the management connection's `endpoints.s3` to match the approved S3 endpoint.

OptiPlex Master/Filer/Volume management ports are used after SSH loop forwards map them to local loopback ports. Do not write Admin passwords, S3 secrets, or signed URLs into reports or the frontend. Real writes are allowed only to random `swc-integration-*` / `swc-management-*` test buckets.

Round19 management boundary: locked `grpcio==1.84.0`, `protobuf==7.36.2`, `fastavro==1.12.2`, `pyarrow==25.0.1`; dev generation tools stay development-only. Mount API has real RPC evidence and currently reports configuredFiler0 for this Filer only, not whole-cluster zero.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q
npm run build --prefix frontend
npm run test:i18n --prefix frontend
```

i18n verification (2026-10-05): the frontend build and 50 bilingual render checks passed. Local browser fixtures covered 15 routes, language persistence, form drafts, session expiry, cross-tab updates, and blocked storage. Object values and table schema names remain unchanged. These checks use synthetic data and do not perform live storage writes. Vite reports a non-blocking JavaScript chunk-size warning.

Real integration uses OptiPlex SeaweedFS 4.48 as specified by the user: `10.34.158.137:8333`. Credentials are fetched through the existing Windows OpenSSH `ssh 10.34.158.137` path into ignored `output/server-secrets.json`, and are not written into reports.

```powershell
.venv\Scripts\python.exe scripts/fetch_test_credentials.py
.venv\Scripts\python.exe scripts/integration_smoke.py --endpoint http://10.34.158.137:8333 --secrets-file output/server-secrets.json --writable-scope --cleanup
```

The scripts create only the current run's random `swc-integration-*` bucket or management-integration-only `swc-management-*` bucket. Test files and bucket configuration are limited to that bucket, and cleanup touches only the current run's created scope. Existing server buckets, production objects, and service configuration are not written. Small-sample tests do not prove production scale at 100k/million objects, real business browser CORS/CDN publication, or reference-protected deletion.

## Implementation references

- [S3 conditional read and version parameters](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html)
- [Pillow decoding and pixel limits](https://pillow.readthedocs.io/en/stable/reference/Image.html)
- [Windows decode process memory limits](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information)
- [SeaweedFS usage materials](https://github.com/seaweedfs/seaweedfs/wiki/Getting-Started)

Passing source and tests does not mean every backend capability is supported. Version/retention/legal hold/bucket configuration are displayed according to actual S3 responses as supported, rejected, unsupported, or unknown. Publication/CDN and business reference protection remain conservatively retained when they are not connected.

## Current local remediation (2026-10-05)

All 15 findings R01–R15 from the deep review have been fixed and locally verified. Backend: 325 passed, 1 warning. Frontend build, bilingual nonempty fixtures, table-preview rendering, simulated state/idempotency checks, and isolated startup/package regressions passed. See [the remediation record](<E:/Project Code/docs/01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-05-29-全面审查修复与本地回归.md>) and `output/fix-review-2026-10-05/`.

S3 secrets require a nonempty approved `allowed_endpoint_url` at creation and use. External management mutations require a fixed idempotency key: resource endpoints use headers, native JSON endpoints use the body, and native uploads use the query. Reuse the key for the same intent and inspect operation history after `needs_review` or a lost response. Unmeasured capacity remains unknown.

Migration audit W01 remains WATCH: initializers and specifically tested compatibility paths are supported locally; a complete named/checksummed migration ledger and arbitrary old-schema upgrade matrix are pending. Test an old database backup in an isolated copy before upgrading. No live service writes, deployment, push, or replacement of historical releases occurred in this remediation. The 2026-10-04 results below are historical evidence.

## Official management mainline history (2026-10-04 baseline)

Test record index: `2026-10-04-28-final-prepackage-auth-ui-311`. The current baseline has completed and verified the SeaweedFS OSS 4.48 management core tasks 28-37. It is not Enterprise capability, a full cross-version protocol replacement, or production full-function verification. The latest management evidence includes read18 all 200, Mount API real RPC configuredFiler0 (current Filer only, not whole cluster), advanced bucket full PASS, S3Tables/Iceberg catalog full real PASS, LANCE dependency-negative, full backend regression 311 passed / 57.69s, measured auth session expiry UX, final Dashboard screenshot `output/management-dashboard-final-1280.png`, auth screenshot `output/auth-session-expired-final.png`, and package privacy scan. The UI595 line keeps using historical valid evidence of docWidth 580 / viewport 595 / sidebar 60 while CSS is unchanged. The new 595 override did not take effect in this round, so no new 595 pass claim is added. Tasks 28-37 are complete.

DataPreview is not logical query, and delete files do not apply. Earlier table-preview real S3 PASS evidence is `output/table-preview-swc-management-tablepreview-b4d0a95b4d.json`. The latest positive S3Tables/Iceberg catalog evidence is `table-catalog92fe388482` full real PASS: on the user-approved owned independent SeaweedFS 4.48 instance `swc-test-catalog-eabfac72`, it used legal `namespace/table/...` layout, ordinary S3 PUT/HEAD/GET 4 files, real RegisterTable, project table-details `native_s3tables_get_table`, and table-preview rows=2 passed with no stub. After cleanup, the catalog was no longer listed, bucket HEAD404, and instance container/remote_dir/SSH pid were cleaned. The latest full backend regression is 311 passed with one existing anyio deprecation warning, duration 57.69s. The latest frontend build passed, latest JS was `index-GlTj8Kzr.js`, stylesheet `index-63Efx6nX.css` was unchanged, `verify_auth_ui.cjs` epoch decision verification passed, table preview SSR PASS, `pip check` clean, and the Cua console had no JS exception. Current package verification passed: 104 payload files + manifest, manifest hashes, archive, excluded private paths, and real credential bytes scan all passed. Two table findings from review were rechecked as APPROVE with no HIGH/MEDIUM. Final Buckets screenshot: `output/management-buckets-1280-final.jpg`; image enhancement regression screenshot: `output/management-images-preserved-1280.jpg`.

Round 23 supplement: IAM task 32 and Worker task 34 were completed according to local implementation/contract evidence. Production global writes are not treated as executed because a mock transport passed. Round 24 supplement: the first real conditional deletion run `4f6be8b674` failed because the QA script preset the unversioned `VersionId None` as the string `null`. After the script was fixed to use the actual HEAD identity, `33e5f48d60` passed, covering unversioned/suspended/null delete/replay/wrong-etag with cleanup HEAD404 confirmed. Latest strong version deletion `398a143767` PASS: oldVersion404, current bytes unchanged, HoldON409, and two bucket cleanup404. Historical `b00f18f764` null501 remains only as old behavior evidence. Task 33 has been independently reviewed as complete with no HIGH/MEDIUM blocker. Round 26 supplement: initial `83c1c3569c` preview 403 came from the old fixture `table/metadata/v1.json`, which had only 3 segments and did not satisfy the pinned `weed/s3api/bucket_paths.go` lines 52-85 requirement for `namespace/table/data|metadata/...` layout with at least 4 segments. It was not evidence that ordinary S3 cannot read from TableBucket, and it was not a proven Reader backend bug. Round 27 supplement: task 33 has been independently reviewed as complete with no HIGH/MEDIUM blocker; the erroneous permanent deletion claim for DeleteMarker=true has been guarded as needs_review, objects29 tests. Round 28 supplement: 35/36/37 are complete, and the official management core is 10/10. LANCE `f247358f2a` real dependency-negative PASS: with no Worker, the system returned dependency_unavailable / TABLE_WORKER_PREVIEW_ALERT, rows/total_rows/deletes_applied were null, and there was also a local positive mock, but real positive Worker has still not run. Production global IAM / Maintenance / MQ / Table writes have not been measured. `codebundle 0.1.0-local` means only that local source code and static assets are available; it is not production deployment. Production global IAM / Volume / Worker / MQ / Table writes have still not been executed.


Package building: use `scripts/package.py --destination output/task-name/bundle.zip`; `--root` optionally selects a project root. The destination must be new, outside packaged source directories and `output/releases`. Help and argument errors write nothing; an existing zip or hash sidecar is rejected. The Python `build_package(root, destination)` API also requires a fresh explicit destination.

Package verification: use `scripts/verify_package.py --package <zip> --registry <approved-registry.json>`; `--registry` can be repeated. The verifier's default targets the historical release, which is not the current remediation snapshot. The builder only reports private path exclusions; `real_credentials=absent` requires actual credential byte scanning. Missing inputs are `not_scanned`, and unreadable or explicitly missing registries fail verification.

Round 30 follows up on the Round 29 review: the management pending gate covers the complete response lifecycle, and malformed or unrecognized successful receipts retain the same intent key and prompt a history/readback check. Only recognized confirmed/failed operation receipts retire the key. Local management-connection CRUD and readonly POSTs retain their normal contracts. See the [follow-up record](<E:/Project Code/docs/01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-05-30-异常回执幂等与打包CLI补充修复.md>) for current validation; Round 29 counts and package hashes remain historical evidence.
