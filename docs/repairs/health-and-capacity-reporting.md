# Report health states and capacity figures without conflating them

Fixes #41 and #42. Both make status or capacity reports state something the evidence does not support.

- **#41: a configured endpoint's 404 is `not_found`.** The native health check (`NATIVE_NOT_FOUND`), the S3 health check (404), the optional admin fetches (`ADMIN_ENDPOINT_NOT_FOUND`) and the native module status all used to report `not_configured`. That also set `endpoint_configured: false` for an endpoint that is configured. `not_configured` is now reserved for endpoints that are actually missing, such as no registry entry or `S3_TABLES_NOT_CONFIGURED`. The management UI shows `not_found` as "Not found" / "未找到" instead of falling back to "Unknown".
- **#41: IPv6 endpoints work.** `_normalize_endpoint` keeps the brackets that `urlsplit().hostname` strips, so `http://[::1]:23646/` normalizes to `http://[::1]:23646`. Before, it became `http://::1:23646`, which httpx rejected with an unhandled 500. An unparseable port is now `422 INVALID_ENDPOINT`. Endpoint approval compares normalized forms on both sides, so it is unchanged.
- **#42: capacity `by_bucket` is keyed `connection_id/bucket`.** Same-named buckets on different connections are no longer summed together; `/` cannot occur in a bucket name. Stored historical samples keep their raw keys.
- **#42: collection `size_bytes` is logical.** Each logical volume ID is counted once, using its largest replica. The physical total is reported separately as `physical_replica_bytes`. If a sized replica has no volume ID, it cannot be de-duplicated, and `size_bytes` is `null` (unknown) instead of a guess.

## Validation

- Full backend suite: **559 passed** (`.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=`).
- `backend/tests/test_reporting_regressions.py` adds 11 cases, of which 9 fail against the previous backend code. The other two guard behavior that must not change: missing endpoints stay `not_configured`, and DNS hostnames still normalize as before.
- Two existing expectations in `test_management_native.py` changed from `not_configured` to `not_found`. Both describe a configured admin endpoint answering 404.
- Frontend: `npm run build`; `npm run test:i18n` gives `I18N_PASS 1448 bilingual messages`; `verify_table_preview_ui.cjs` and `verify_frontend_review_regressions.cjs` report `failures: []`.

These are fake-admin and fake-S3 results only. No live SeaweedFS IPv6 endpoint or replicated collection was measured.
