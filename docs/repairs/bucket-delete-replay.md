# Replay bucket deletion receipts before remote preflight

Retrying a confirmed bucket deletion with the same idempotency key tried to list the already-deleted bucket before replaying its receipt.

Perform the emptiness check inside the journal-controlled invocation, after replay and conflict resolution.

## Verification

Real SQLite journal with mocked S3 verifies successful replay, no remote call on missing key, and conflicting retries without dispatch.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/bucket-delete-replay/backend:/workspace/swc-prs/bucket-delete-replay /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_resources.py -q -p no:cacheprovider -o addopts=
```

Result: **24 passed, 1 warning in 1.73s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
