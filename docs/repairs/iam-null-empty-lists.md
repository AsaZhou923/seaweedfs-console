# Recognize explicit empty IAM list readbacks from OSS 4.48

OSS 4.48 emits empty actions/policy_names slices as null, causing valid user changes to be marked needs_review.

Normalize explicit null only for the two known list fields when the requested value is an empty list. Missing fields, wrong types, nonempty requests, and other fields remain strict.

## Verification

Upstream-shaped null readback for create/update, generated-secret delivery and replay redaction, plus absence/type/identity mismatches.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/iam-null-empty-lists/backend:/workspace/swc-prs/iam-null-empty-lists /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_resources.py -q -p no:cacheprovider -o addopts=
```

Result: **36 passed, 1 warning in 2.75s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
