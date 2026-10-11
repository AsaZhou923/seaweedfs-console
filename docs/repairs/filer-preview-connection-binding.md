# Bind recursive delete previews to their management connection

Identical Filer paths and entries produced a reusable recursive deletion preview hash across management connections.

Include the management connection ID and approved Filer endpoint in recursive preview identity at generation and verification.

## Verification

A preview from another connection is rejected without deletion; each connection receives a distinct hash. This does not add atomic remote compare-and-swap.

Tested in this issue branch independently of the combined repair checkout:

```bash
PYTHONPATH=/workspace/swc-prs/filer-preview-connection-binding/backend:/workspace/swc-prs/filer-preview-connection-binding /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_management_native.py -q -p no:cacheprovider -o addopts=
```

Result: **36 passed, 1 warning in 7.36s**. Mocked HTTP/storage adapters and disposable local SQLite; this branch run does not establish live deployment acceptance. The shared interpreter imported application modules from this branch, verified separately.

Base: `origin/main` at `d721727d7450aea7f8b7fbececb94df824ce2fa8`. No dependent repair branch.
