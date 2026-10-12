# Recheck Filer target roots immediately before write dispatch

A Filer root revoked during remote pre-write readback could still be written using the initial policy snapshot.

Validate mkdir/upload/delete targets and both rename paths against each latest persisted management policy guard, including the guard immediately before dispatch.

## Verification

Revocation during readback blocks all four operation types and either rename boundary; approved writes and durable replay still work.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/filer-latest-write-roots/backend:/workspace/swc-prs/filer-latest-write-roots /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_ops.py backend/tests/test_management_native.py -q -p no:cacheprovider -o addopts=
```

Result: **57 passed, 1 warning in 7.43s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
