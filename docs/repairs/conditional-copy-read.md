# Protect copy reads with source ETag conditions

Bind source reads to the observed ETag and exact version before any target write. Reject adapters without conditional reads or objects without an ETag, closing replacement-and-restoration races that later HEAD checks cannot detect.

## Scope

Independent repair based on `d721727d7450aea7f8b7fbececb94df824ce2fa8`; no prerequisite repair branches.

## Adapter contract

Safe-copy adapters must provide `get_object_bytes_if_match(bucket, key, version_id, etag)`. The S3 adapter sends the ETag as `IfMatch` and preserves the selected version. The focused tests verify request construction and simulated precondition failures with synthetic adapters; they do not prove that a particular upstream S3 service enforces `IfMatch`.

## Validation

- Enhancement API and domain suites: **50 passed in 0.61s**.

```sh
PYTHONPATH=/workspace/swc-prs/conditional-copy-read/backend /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_enhancement_api.py backend/tests/test_enhancements.py -q -p no:cacheprovider -o addopts=
```

These are synthetic-adapter/API tests run from this independent branch; no claim of branch-specific live-service acceptance.
