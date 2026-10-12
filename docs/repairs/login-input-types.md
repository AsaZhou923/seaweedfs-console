# reject malformed login credentials

Non-string login credentials reached SQLite/password verification and raised an unhandled exception. Reject them uniformly with 401 before session creation.

Regression coverage: `test_login_rejects_malformed_credentials_without_session`.

These tests use local FastAPI/SQLite and synthetic storage fixtures. They do not establish production acceptance.
