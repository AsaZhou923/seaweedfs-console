# Make derived-variant batches submittable from the UI and reconcile their status

Fixes #35 and #49, the create and complete halves of the same batch lifecycle.

- Default batch keys are `output_scope.prefix + output_prefix + <seed>.<format>`. `output_prefix` defaults to `derived/` and stays literal: slashes are neither added nor stripped.
- `snapshot_id` batches resolve the pinned `operation_snapshots` row. The row must belong to this Scope and actor, match the current `authz_epoch`, and not have expired. Every object must still have its pinned revision. Otherwise the request is rejected with `SNAPSHOT_NOT_FOUND`, `SNAPSHOT_STALE`, `SNAPSHOT_EXPIRED` or `OBJECT_REVISION_CHANGED`, before anything is written.
- The batch row, its variants and its jobs are written in one `BEGIN IMMEDIATE` transaction. A same-key replay of a legacy `planning` batch finishes planning it instead of returning it empty. It reuses the persisted item plan (bindings and output keys) when one was stored, so earlier queued jobs replay instead of conflicting.
- The UI generates the required `Idempotency-Key` when the field is empty and keeps it for retries. Changing the batch inputs clears it, and the returned batch id is filled in.
- Batch `status` is derived from its items on every read: `queued`, `running`, `succeeded`, `partially_failed`, `failed` or `needs_review`. A `planned` variant whose job is already terminal is reported with the job state; stored rows are not backfilled.
- If a variant job fails before its dispatch checkpoint, the variant becomes `failed`. If it fails after the checkpoint, the variant becomes `needs_review`. In both cases `last_error` holds the error code. Only the current lease holder writes, and only a `planned` row changes.
- `jobs.run_once` keeps `StorageError` and enhancement error codes instead of `WORKER_FAILURE`.

## Validation

- Backend: **473 passed** (`.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=`). This includes 15 new or updated cases in `backend/tests/test_enhancement_api.py`.
- Frontend: `npm run build` passed; `npm run test:i18n` gives `I18N_PASS 1438 bilingual messages`. `node scripts/verify_frontend_review_regressions.cjs` reports `failures: []` against the fully mocked local service.

These are synthetic-adapter, API and mocked-browser results. They make no claim of live-service acceptance; `scripts/integration_smoke.py` was updated for the literal `output_prefix` but was not run.
