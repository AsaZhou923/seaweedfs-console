# Keep scan observations real and bound the missing-candidate phase

Fixes #33 and #34, both in the scan loop of `catalog._scan`.

- **#33: unreadable objects keep their real identity.** When HEAD, GET or decode fails (including every object over the 32 MiB read budget), the scan indexes the HEAD observation if HEAD succeeded, otherwise the already-normalized listing entry. It no longer re-normalizes the listing, which dropped size, ETag and Last-Modified and indexed a fake `size=0` revision. `enhancement_api._identity` keeps a real zero-byte `ContentLength`/`Size` instead of falling through to `None`.
- **#34: the missing-candidate phase is bounded.** Every candidate HEAD is preceded by a lease refresh and a pause/cancel check. The checkpoint records `phase: confirm_missing` and the candidate cursor, so a resumed scan continues the phase without relisting. HEADs are rate-limited by `objects_per_second`. A single rate-limit wait, in this phase and in the listing loop, is capped at 60 s, half the 120 s lease, so a very low rate cannot outlive the lease.
- **#34: candidates that still exist are restored.** A candidate that still answers HEAD is set back to `present` when its revision is unchanged. If it changed, the new revision is recorded with `decode_status: unknown`. It is no longer HEADed again on every scan. Only an authoritative 404 confirms a deletion.

Not changed: `objects.size` stays `NOT NULL DEFAULT 0`. Making it nullable needs a schema migration and is outside this repair.

## Validation

- `backend/tests/test_catalog_regressions.py`: 6 new scan regressions, including a low-rate lease bound. The first 5 fail against the previous `catalog.py` (5 failed, 5 passed); with this change, all 11 tests pass.
- `backend/tests/test_enhancement_api.py`: a zero-byte identity regression.
- Full backend suite: **481 passed** (`.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=`).

These are fake-S3 results only; no live SeaweedFS scan was run.
