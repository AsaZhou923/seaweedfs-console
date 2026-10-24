# Create, delete and roll back bucket configuration from the empty state

Fixes #47 and #48. Both break when a bucket config is empty (the normal state for a new bucket): the Scope path could never create the first config, and the management path misread the empty state before and after writes.

- **#48: Scope bucket config.** `GET /scopes/{id}/bucket/{cors|lifecycle|policy}` reads S3 `NoSuch*Configuration` / `NoSuchBucketPolicy` (the `NOT_CONFIGURED` error from #50) as `config: {}`. It returns `configuration_state: "not_configured"` and a stable `current_hash`, so the UI can save the first config. Specifically:
  - `update_bucket_config` and `rollback_bucket_config` read the before and readback states the same way.
  - Rolling back to a not-configured snapshot calls `DeleteBucket{Cors,Lifecycle,Policy}`, because S3 has no "put empty".
  - Saving an empty config is rejected.
  - After the write is sent, only a definite storage refusal is `failed`; a lost response or 5xx is `needs_review`.
- **#47: management bucket settings.** A DELETE of a policy or lifecycle is confirmed from the 4.48 admin readback shapes `{"bucket": b, "policy": null}`, `{"bucket": b, "rules": []}` and `{"bucket": b, "lifecycle": {"rules": []}}`. Only identifying keys may sit beside the emptied field, so leftover content never counts as deleted.
- **#47: S3-backed readbacks.** The versioning and object-lock readbacks treat S3 "not configured" as an empty snapshot (`{"ObjectLockConfiguration": {}}`). Enabling object lock on a bucket without it is no longer aborted before dispatch. Other readback failures become a typed `S3_CONFIGURATION_UNAVAILABLE` / `UNSUPPORTED_CAPABILITY` error with safe detail, instead of an opaque `MANAGEMENT_OPERATION_FAILED`.

## Validation

- Full backend suite: **543 passed** (`.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=`).
- New regressions are in `test_management_resources.py` (4.48 wrapped delete shapes, verifier rejections, first object-lock config, typed before-read failure), `test_enhancements.py` (not-configured read, first create, rollback to not configured, empty save, outcome split) and `test_enhancement_api.py` (GET `not_configured` for each kind, adapter delete mapping). Against the previous backend code, 20 of them fail.
- `npm run test:i18n`: `I18N_PASS 1447 bilingual messages`. `npm run build` passed.

These are fake-S3 / fake-admin results using the response shapes recorded in the issues. No live SeaweedFS bucket configuration was written. The Scope rollback-to-empty path relies on S3 `DeleteBucketCors`, `DeleteBucketLifecycle` and `DeleteBucketPolicy`, which this change did not verify against a live 4.48 server.
