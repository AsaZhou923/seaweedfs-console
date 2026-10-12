# Require exact empty-table evidence before deletion

Substring table lookup and permissive count coercion could select unrelated emptiness evidence; malformed inventories could falsely confirm deletion, and retries performed preflight before receipt replay.

Require exactly one matching table name and consistent observed zero counts before dispatch. Confirm absence only from a valid inventory, and resolve durable replay before re-reading the target.

## Verification

Nonempty/similarly named/ambiguous/unknown targets, invalid counts, changes during readback, confirmed receipt replay, and malformed post-delete inventories.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/table-delete-exact-target/backend:/workspace/swc-prs/table-delete-exact-target /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_native.py -q -p no:cacheprovider -o addopts=
```

Result: **48 passed, 1 warning in 9.23s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
