from __future__ import annotations

import pytest

from console import storage


class ClientError(Exception):
    def __init__(self, status: int | None, code: str | None = None):
        metadata = {} if status is None else {"HTTPStatusCode": status}
        self.response = {"Error": {"Code": code} if code else {}, "ResponseMetadata": metadata}


@pytest.mark.parametrize(
    ("status", "code", "expected_code", "expected_status"),
    [
        (404, "NoSuchCORSConfiguration", "NOT_CONFIGURED", 404),
        (404, "NoSuchLifecycleConfiguration", "NOT_CONFIGURED", 404),
        (404, "NoSuchBucketPolicy", "NOT_CONFIGURED", 404),
        (404, "ObjectLockConfigurationNotFoundError", "NOT_CONFIGURED", 404),
        (400, "NoSuchObjectLockConfiguration", "NOT_CONFIGURED", 404),
        (404, "NoSuchKey", "OBJECT_NOT_FOUND", 404),
        (400, "InvalidRequest", "STORAGE_REJECTED", 400),
        (409, "OperationAborted", "STORAGE_REJECTED", 409),
        (403, "AccessDenied", "STORAGE_FORBIDDEN", 403),
        (412, "PreconditionFailed", "STORAGE_PRECONDITION_FAILED", 412),
        (501, "NotImplemented", "STORAGE_UNSUPPORTED", 501),
        (400, "NotImplemented", "STORAGE_UNSUPPORTED", 501),
        (408, "RequestTimeout", "STORAGE_UNAVAILABLE", 503),
        (500, "InternalError", "STORAGE_UNAVAILABLE", 503),
        (None, None, "STORAGE_UNAVAILABLE", 503),
    ],
)
def test_sanitize_keeps_not_configured_rejected_and_unavailable_apart(status, code, expected_code, expected_status):
    error = storage.sanitize_storage_exception(ClientError(status, code))
    assert (error.code, error.status) == (expected_code, expected_status)


def test_sanitize_passes_through_an_already_sanitized_error():
    original = storage.StorageError("STORAGE_FORBIDDEN", "Storage denied the request.", 403)
    assert storage.sanitize_storage_exception(original) is original
    assert storage.sanitize_storage_exception(RuntimeError("socket closed")).code == "STORAGE_UNAVAILABLE"


@pytest.mark.parametrize(
    ("error", "rejected"),
    [
        (storage.StorageError("STORAGE_REJECTED", "x", 400), True),
        (storage.StorageError("STORAGE_FORBIDDEN", "x", 403), True),
        (storage.StorageError("STORAGE_PRECONDITION_FAILED", "x", 412), True),
        (storage.StorageError("STORAGE_UNSUPPORTED", "x", 501), True),
        (storage.StorageError("STORAGE_UNAVAILABLE", "x", 503), False),
        (storage.StorageError("STORAGE_UNAVAILABLE", "x", 408), False),
        (RuntimeError("read timed out"), False),
    ],
)
def test_known_rejection_only_for_definite_refusals(error, rejected):
    assert storage.is_known_rejection(error) is rejected
