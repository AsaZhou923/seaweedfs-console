# Reject malformed management usernames and status values

Truthy non-string usernames and unhashable status values raised unhandled errors before validation.

Require a nonempty username string and string status values before constructing paths or checking enum membership.

## Verification

Malformed usernames and bucket/IAM status values return 422 without remote calls or operation dispatch.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/management-input-validation/backend:/workspace/swc-prs/management-input-validation /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_resources.py -q -p no:cacheprovider -o addopts=
```

Result: **44 passed, 1 warning in 3.77s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
