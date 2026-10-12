# Bind reference manifest entries to their Scope connection

Reject reference manifest entries that claim another connection and fill the authoritative Scope connection before hashing. Both API validation and manifest import enforce the identity binding.

## Scope

Independent repair based on `d721727d7450aea7f8b7fbececb94df824ce2fa8`; no prerequisite repair branches.

## Validation

- Enhancement API and domain suites: **45 passed in 0.62s**.

- `npm run test:i18n --prefix frontend`: passed on this branch.

```sh
PYTHONPATH=/workspace/swc-prs/manifest-connection-identity/backend /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_enhancement_api.py backend/tests/test_enhancements.py -q -p no:cacheprovider -o addopts=
```

These are synthetic-adapter/API tests run from this independent branch; no claim of branch-specific live-service acceptance.
