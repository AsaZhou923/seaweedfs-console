# Recheck multipart Scope permissions after receiving part bytes

Multipart part uploads recheck current Scope authorization after consuming the request stream. Archival, write revocation, or authorization identity changes stop dispatch before storage access.

## Scope

Independent repair based on `d721727d7450aea7f8b7fbececb94df824ce2fa8`; no prerequisite repair branches.

## Validation

- Enhancement API and domain suites: **47 passed in 0.47s**.

```sh
PYTHONPATH=/workspace/swc-prs/multipart-permission-refresh/backend /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_enhancement_api.py backend/tests/test_enhancements.py -q -p no:cacheprovider -o addopts=
```

These are synthetic-adapter/API tests run from this independent branch; no claim of branch-specific live-service acceptance.
