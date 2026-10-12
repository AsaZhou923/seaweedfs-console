# Keep metadata copies bound to the selected source revision

Pass the indexed source revision into the copy helper so a source replacement after the initial metadata HEAD cannot be copied to the target as a successful operation.

## Scope

Independent repair based on `d721727d7450aea7f8b7fbececb94df824ce2fa8`; no prerequisite repair branches.

## Validation

- Enhancement API and domain suites: **45 passed in 0.61s**.

```sh
PYTHONPATH=/workspace/swc-prs/metadata-copy-revision/backend /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_enhancement_api.py backend/tests/test_enhancements.py -q -p no:cacheprovider -o addopts=
```

These are synthetic-adapter/API tests run from this independent branch; no claim of branch-specific live-service acceptance.
