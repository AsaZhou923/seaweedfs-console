# Reset explicitly empty management permission policies

An explicit empty permissions object was treated as omitted, retaining previous write grants.

Distinguish an omitted policy from an explicitly supplied empty object. Empty objects apply default deny-write permissions; present non-object values are rejected.

## Verification

Policy persistence, omitted-policy retention, explicit revocation, and invalid falsey input without access-event side effects.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/management-empty-permissions/backend:/workspace/swc-prs/management-empty-permissions /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management.py -q -p no:cacheprovider -o addopts=
```

Result: **27 passed, 1 warning in 5.31s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
