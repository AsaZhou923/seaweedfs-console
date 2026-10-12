# filter job history by Scope before limiting

More than 200 newer jobs in another Scope hid the selected Scope's history. Authorize and filter the requested Scope in SQL before the limit, with deterministic ordering.

Regression coverage: `test_scope_jobs_are_filtered_before_limit`.

These tests use local FastAPI/SQLite and synthetic storage fixtures. They do not establish production acceptance.
