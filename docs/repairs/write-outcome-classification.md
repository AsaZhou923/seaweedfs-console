# Classify write outcomes: failed only when nothing was written

Fixes #36, #37 and #50. All three record a write as `failed`, `needs_review` or a misleading error state when the evidence says otherwise.

- **#50: S3 error mapping.** `storage.sanitize_storage_exception` maps by S3 error code as well as by HTTP status:
  - `NoSuch*Configuration`, `NoSuchBucketPolicy` and object-lock not-found codes become `404 NOT_CONFIGURED`.
  - `NotImplemented` and other unsupported codes, or a 501, become `501 STORAGE_UNSUPPORTED`.
  - Other 4xx answers (except 408) become `STORAGE_REJECTED` with their real status.
  - Only 5xx, 408 and missing responses stay `503 STORAGE_UNAVAILABLE`.
  - An already-sanitized `StorageError` passes through unchanged.
  - The CORS read diagnostic reports `not_configured`.
- **#50: safe copy.** In `copy_object_safely`, a 412 on the `IfNoneMatch=*` put becomes a terminal `failed` with `409 TARGET_EXISTS`. Failures before dispatch and definite put refusals are also `failed`. Only failures after dispatch or during verification stay `needs_review`.
- **#36: retention and legal hold.** Every retention or legal-hold write first checks `GetObjectLockConfiguration` live on the Scope's bucket. The write is refused before any intent unless `ObjectLockEnabled` is `Enabled`; otherwise it returns `409 OBJECT_LOCK_NOT_CONFIGURED`, `501 UNSUPPORTED_CAPABILITY` or `409 OBJECT_LOCK_UNKNOWN`. This replaces the broken stored-capability check. A definite refusal of the first write is `failed`. Lost responses, 5xx errors, partial writes and failed readbacks are `needs_review`, returned as `502 RETENTION_OUTCOME_UNKNOWN` with the intent id.
- **#37: copy batches.** A copy-batch item is `failed` only before dispatch or when storage definitely refused the put. Any error after dispatch, including target HEAD or read errors during verification, is `needs_review`.

New owned messages are added to the English and Chinese API catalogs.

## Validation

- Full backend suite: **523 passed** (`.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=`).
- New regressions are in `test_storage_errors.py`, `test_enhancements.py`, `test_copy_batches.py` and `test_enhancement_api.py`. Against the previous backend code, 31 of them fail.
- `npm run test:i18n`: `I18N_PASS 1446 bilingual messages`. `npm run build` passed.

These are fake-S3 results only. No live SeaweedFS retention, legal-hold or copy write was run.
