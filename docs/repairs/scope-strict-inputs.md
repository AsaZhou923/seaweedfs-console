# validate Scope flags and policy types

A string such as "false" enabled Scope privileges because it was truthy. Require actual booleans and correctly typed prefix/policy containers before persistence; localize the resulting validation errors.

Regression coverage: `test_scope_creation_rejects_malformed_privileges_and_prefix`.

These tests use local FastAPI/SQLite and synthetic storage fixtures. They do not establish production acceptance.
