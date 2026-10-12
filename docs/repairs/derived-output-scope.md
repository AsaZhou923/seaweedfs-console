# Keep generated derived image keys inside the output Scope

Default derived image keys include the output Scope literal prefix. Validate the generated target before enqueueing and recheck current target permissions before the worker accesses storage.

## Scope

Independent repair based on `d721727d7450aea7f8b7fbececb94df824ce2fa8`; no prerequisite repair branches.

## Validation

- Enhancement API and domain suites: **48 passed in 0.86s**.

```sh
PYTHONPATH=/workspace/swc-prs/derived-output-scope/backend /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_enhancement_api.py backend/tests/test_enhancements.py -q -p no:cacheprovider -o addopts=
```

These are synthetic-adapter/API tests run from this independent branch; no claim of branch-specific live-service acceptance.
