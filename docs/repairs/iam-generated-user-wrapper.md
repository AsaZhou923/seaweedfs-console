# Verify generated credentials in the OSS user response wrapper

OSS create-user responses place generated credentials under user, so top-level-only key lookup could not confirm a successful creation.

Read the known user wrapper only, require its username to match the target, and verify the returned public key appears in the target readback. Preserve one-time secret delivery and redacted persistence.

## Verification

Exact nested response shape, secret delivery/replay/journal checks, and malformed or unrelated wrapper, username, and key mismatches.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/iam-generated-user-wrapper/backend:/workspace/swc-prs/iam-generated-user-wrapper /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_resources.py -q -p no:cacheprovider -o addopts=
```

Result: **30 passed, 1 warning in 2.90s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
