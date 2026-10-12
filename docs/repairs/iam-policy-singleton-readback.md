# Compare IAM policy scalar and singleton action forms semantically

OSS normalizes singleton Action/Resource arrays to strings, making exact JSON equality mark successful policy writes needs_review.

Normalize only Statement action/resource scalar-or-list fields during IAM and bucket policy comparison; preserve all other values and the original request/readback evidence.

## Verification

Both scalar/list directions, meaningful field and condition mismatches, target identity, and an upstream-shaped HTTP response with the real operation journal.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/iam-policy-singleton-readback/backend:/workspace/swc-prs/iam-policy-singleton-readback /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_resources.py -q -p no:cacheprovider -o addopts=
```

Result: **42 passed, 1 warning in 2.88s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
