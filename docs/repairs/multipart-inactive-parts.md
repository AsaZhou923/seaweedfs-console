# Reject multipart parts for inactive uploads

Recheck upload lifecycle after receiving part bytes and reject nonactive sessions before remote dispatch, including an upload aborted during request streaming.

Independent main-based repair. Validation on this branch: 53 passed in 0.52s. Synthetic-adapter/API tests, not branch-specific live-service acceptance.

```sh
PYTHONPATH=/workspace/swc-prs/multipart-inactive-parts/backend /workspace/seaweedfs-console/.venv/bin/python -m pytest backend/tests/test_enhancement_api.py backend/tests/test_enhancements.py -q -p no:cacheprovider -o addopts=
```

`npm run test:i18n --prefix frontend`: passed.
