# SeaweedFS Console Project Guidelines

These guidelines apply to the project root and its subdirectories. Follow the parent workspace instructions; deeper `AGENTS.md` files may add local rules. Explicit user instructions and existing authorization take precedence, especially review-only requests and restrictions on deployment, pushing, or historical data changes.

<!-- AUTONOMY DIRECTIVE — DO NOT REMOVE -->
Complete authorized local changes, related fixes, and verification without repeatedly asking whether to proceed. Try a safe alternative when blocked; ask only for necessary missing information or authority. Use Codex native subagents for independent, verifiable parallel tasks when they improve quality or throughput. Handle simple tasks directly.
<!-- END AUTONOMY DIRECTIVE -->

## Product and Scope

- The primary product is a third-party SeaweedFS OSS Admin enhancement console. The image asset workbench is an enhancement module. Keep management connections and project storage Scopes distinct in both concepts and permissions.
- Official interfaces follow the project's pinned SeaweedFS 4.48 source baseline; see the commit in `docs/management-api.md`. Check official source or documentation before adding an adapter. An upstream route does not prove support in the target deployment.
- The development plan controls current delivery scope and status. Routine fixes do not automatically expand into deferred tasks 23–27, Enterprise features, multiple user roles, complete cross-version protocols, or production global-write acceptance.
- Distinguish unknown, not configured, rejected, unsupported, and observed zero. Do not invent capacity, health, reference state, timestamps, or completion percentages.

## Documentation and Layout

| Location | Responsibility |
| --- | --- |
| `backend/console/main.py`, `core.py`, `security.py`, `config.py`, `db.py` | API entry point, identity/Scope checks, configuration, and SQLite foundation |
| `backend/console/storage.py`, `catalog.py`, `jobs.py`, `imaging.py` | Storage adapters, indexing, standalone worker, and bounded image decoding |
| `backend/console/enhancement_api.py`, `enhancements.py`, `batch_operations.py` | Derived images, object operations, references, capacity, and batches |
| `backend/console/management*.py`, `table*.py` | Management connections, durable intents, native/resource/object APIs, gRPC, and table previews |
| `frontend/src/main.tsx`, `management/`, `i18n/` | SPA, management views, and English/Chinese catalogs |
| `backend/tests/`, `tests/scripts/` | Backend behavior and startup/package regressions |
| `scripts/` | Startup, shutdown, backup, packaging, verification, and integration scripts with distinct authorization requirements |
| `data/`, `output/` | Runtime data, private credentials, logs, historical packages, and verification artifacts; preserve existing contents |

Read the documentation needed for the task; do not scan all historical material for a small change:

- Operation, configuration, and recovery: `README.md` / `README.zh-CN.md`.
- UI decisions: `DESIGN.md`. API and transport contracts: `docs/management-api.md`.
- **Single development progress ledger:** [Development plan](<E:/Project Code/docs/01 - Projects/seaweedfs-console/08 - Plans/seaweedfs-console 开发计划.md>).
- Architecture and implementation contracts: [System design](<E:/Project Code/docs/01 - Projects/seaweedfs-console/02 - Architecture/seaweedfs-console 系统设计.md>) and [Detailed technical design](<E:/Project Code/docs/01 - Projects/seaweedfs-console/02 - Architecture/seaweedfs-console 详细技术设计.md>).
- Round-specific evidence: `docs/test-fix-log.md` and the vault's `05 - Testing/Records/`; reviews are in `10 - Reviews/`. `docs/progress.md` is a pointer, not a second completion ledger.

The documentation vault root is `E:\Project Code\docs\01 - Projects\seaweedfs-console`. These absolute paths identify resources on this machine. If unavailable, first check pointers in project documentation; report missing material contracts rather than claiming to have read them. Preserve existing document filenames and link targets, including Chinese filenames.

## Development and Execution

- Keep the existing React/TypeScript/Vite, FastAPI, standard-library sqlite3, SQLite WAL, and standalone worker architecture. Reuse utilities and domain logic, and keep diffs small and reviewable. Do not add dependencies, an ORM, or a task framework without a request.
- Inspect working state before code changes. If Git metadata is absent, do not initialize a repository or invent commits/diffs. Save a task baseline for affected files, compare hashes or text, and preserve unrelated work.
- Preserve lockfiles and existing formatting. Verify paths and the shell at Windows/WSL boundaries; avoid CRLF noise. Use task-specific PowerShell variables rather than overwriting system variables.
- Route shell commands through RTK: prefer supported commands, otherwise use `rtk proxy`. Search with `rtk proxy rg`. Run compound PowerShell operations through `rtk proxy pwsh -NoProfile -Command ...`. Do not use `omx explore` or stack OMX shell wrappers.
- If RTK is unavailable, report it and use native tools. Do not install or repair RTK/OMX for ordinary project work. Only explicitly selected workflows maintain their own `.omx/` state.
- Assign clear file/responsibility ownership to parallel agents, tell them they share the codebase, and prohibit overwriting others' edits. Inherit model settings by default. Use executor for ordinary implementation; reserve worker for an explicitly selected Team runtime.

## Required Behavioral Contracts

- **Credentials and authorization:** Secrets stay in the backend registry. S3 secrets require a nonempty approved `allowed_endpoint_url`, checked at creation and every use; connection-ID binding does not replace endpoint approval. Admin/native/gRPC adapters use explicitly approved endpoints only. Never log plaintext secrets, sessions, or signed URLs.
- **Scope and object identity:** Check Scope and action permissions on every backend request. Scope prefixes are literal prefixes; do not append slashes or normalize them as paths. Bind copies to the selected index revision, check source identity before target side effects, and retain conditional protection during reads.
- **Management writes:** Require the write switch, action permission, and necessary confirmations. Persist intent before dispatch, then verify the exact target through readback. Missing stable idempotency keys must not dispatch; the same key and intent replay, conflicts return 409. Keep the pending gate through body reading, parsing, and receipt validation. Retire an intent key only for a recognized terminal operation receipt; malformed, empty, or unrecognized responses remain uncertain. After `needs_review` or a lost response, inspect history rather than blindly resending or rotating keys.
- **Key transport:** Resource APIs use the `Idempotency-Key` header, with `X-Idempotency-Key` compatibility. Native JSON writes use body `idempotency_key`; native uploads use the query. Keep the frontend native allowlist aligned with new endpoints; do not inject keys into strict Bucket/IAM business JSON. Exclude login, logout, local CRUD, and readonly POSTs from durable intents. Do not persist password-derived fingerprints or sensitive payloads.
- **Asynchronous UI:** Invalidate old lists, details, selections, jobs, and results when Scope/query/connection or table request identity changes. Old requests and polls must not overwrite the new context. Assets and Tables regressions need actual delayed/reversed responses; disable export without a Scope.
- **Capacity and references:** Successful derivation stores verified output size. Keep unmeasured fields null/unknown through threshold evaluation. Preserve raw historical snapshot values; use measurement/legacy_unknown to isolate unproven zeros, and sort equal timestamps deterministically. Validate reference entries and fill authoritative bucket/connection identity before hashing. Disconnected business references remain unknown; a complete scan does not prove source images are safe to delete.
- **SQLite and recovery:** Preserve WAL, foreign keys, and short transactions. Keep network calls and decoding outside transactions. Use the SQLite backup API and restore to a new target; do not copy only a running DB file. Check W01 migration-audit status against the latest plan. Local compatibility tests do not prove arbitrary old-database upgrades; do not backfill or clean historical business data.
- **Startup and shutdown:** Clean up only parent/child processes created by this attempt whose ownership is verified. Register ownership before reading fallible metadata. Parent exit or PID reuse must not authorize fresh children. A cleanup failure must not hide the original error. Never kill globally by port or process name.
- **Language:** English is the default project and UI language. Write project-level agent guidance in English. Maintain both English and Chinese for owned UI text, buttons, errors, populated rows, and conditional titles/placeholders. Preserve object keys, user names, real column names, and raw API evidence.

## Verification Commands

Run these commands from the project root. Start with affected, narrow regressions and expand according to risk. Reuse valid evidence for unchanged inputs rather than repeatedly pursuing repository-wide zero errors.

```powershell
# Broad backend behavior changes or final regression
rtk proxy .venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=

# Startup or packaging script changes
rtk proxy .venv\Scripts\python.exe -m pytest tests/scripts -q -p no:cacheprovider -o addopts=

# Frontend logic, types, or UI changes
rtk proxy npm run build --prefix frontend
rtk proxy npm run test:i18n --prefix frontend
rtk proxy node scripts/verify_table_preview_ui.cjs
rtk proxy node scripts/verify_frontend_review_regressions.cjs

# Dependency compatibility after dependency changes
rtk proxy .venv\Scripts\python.exe -m pip check
```

Frontend dynamic regressions use the built `frontend/dist` and a fully mocked local service, requiring available Chrome/Edge on this machine. Report unavailable tooling rather than substituting real service writes. For documentation or wording changes, check content, commands, and links without running unrelated application suites.

Add meaningful regressions for risky permission, remote-write, object-identity, migration, or race behavior. Cover failure paths, absence of side effects, replay/conflicts, populated data, and affected consumers. Fix failures caused by the change; report unrelated existing failures or unavailable tools honestly.

## External Operations and Packaging Boundaries

- Local-edit authorization does not authorize deployment, pushing, real storage writes, or production global changes. Continue within existing explicit authorization without asking again.
- Do not run `integration_*.py`, `ui_fixture.py`, `seed_management_preview.py`, `s3tables_test_instance.py`, or credential-fetching scripts by default. Read them first to establish real-service access, credentials, and side effects, then act within the task's authorization.
- Authorized live tests operate only on this run's owned random `swc-integration-*` / `swc-management-*` buckets/prefixes and explicitly approved instances. Establish separate scope for global IAM/Volume/Worker/MQ/Table writes. Cleanup touches only resources recorded as owned by this run.
- Preserve historical packages in `output/releases/`. Use a new task directory and explicit destination for local verification. `scripts/package.py` requires `--destination` and rejects missing/unknown arguments, existing zip or sidecar targets, historical release destinations, and destinations inside packaged source directories. The Python builder also requires an explicit fresh destination.
- The builder proves private-path exclusion only, not absence of credential bytes. Explicitly select the current package and scan actual registries with the verifier. Never report `not_scanned` as credentials absent.

```powershell
# Replace example paths with this run's artifact and approved registry; --registry is repeatable
rtk proxy .venv\Scripts\python.exe scripts/package.py --destination output/task-name/bundle.zip
rtk proxy .venv\Scripts\python.exe scripts/verify_package.py --package output/task-name/bundle.zip --registry output/task-name/approved-registry.json
```

When changing packaging logic, verify both READMEs, internal relative links, manifest/hashes, and synthetic leakage cases from root/configured/external registries. Never copy real secrets into tests or verification artifacts.

## Documentation Updates and Delivery

- When changing interfaces, permissions, capacity/identity semantics, operation, or verification, update relevant READMEs, API/design documents, and corresponding development-plan notes. Save round-specific evidence in that round's test record.
- Preserve original reviews, old test results, historical screenshots, and release packages. Add current remediation results without rewriting historical PASS or REQUEST CHANGES. Maintain test counts, package hashes, and completion status in their records and the single ledger, not as fixed values in this file.
- Report changed files, verification and artifact locations, and material incomplete or unverified boundaries. Separate mock results, local builds, and historical live evidence; do not label them production acceptance.
- Commit or publish only when authorized by the user. Lead commit descriptions with why the change is needed, handle explicit project paths only, and preserve unrelated files and history.
