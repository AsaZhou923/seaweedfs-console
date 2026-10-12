# avoid nested session writes during job control

Pause/resume/cancel authenticated again after taking a SQLite write lock, causing a second session update to block on the same operation. Reuse the authenticated request and check Scope permission on the existing transaction.

Regression coverage: `test_job_controls_with_real_session_and_repeated_requests`, `test_job_control_checks_scope_and_csrf_before_changing_state`.

These tests use local FastAPI/SQLite and synthetic storage fixtures. They do not establish production acceptance.
