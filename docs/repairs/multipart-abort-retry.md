# Allow multipart abort retry after a transient failure

Allow abort_failed sessions to retry cleanup, replay successful aborts without a remote call, clear stale errors on success, and record aborted_at only on success.

Independent main-based repair. Validation on this branch: 45 passed in 0.45s. Synthetic-adapter/API tests, not branch-specific live-service acceptance.

```sh
PYTHONPATH=/workspace/swc-prs/multipart-abort-retry/backend /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_enhancement_api.py backend/tests/test_enhancements.py -q -p no:cacheprovider -o addopts=
```
