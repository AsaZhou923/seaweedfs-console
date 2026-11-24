from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from scripts.integration_conditional_delete import require_rejected_null_protection_read


def error(code, status):
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObjectRetention")


def test_unsupported_evidence_requires_an_exact_version_rejection():
    target = {"Bucket": "test", "Key": "folder/object", "VersionId": "null"}
    s3 = Mock()
    s3.get_object_legal_hold.side_effect = error("NoSuchObjectLegalHold", 404)
    s3.get_object_retention.side_effect = error("InvalidRequest", 400)
    require_rejected_null_protection_read(s3, target)
    s3.get_object_legal_hold.assert_called_once_with(**target)
    s3.get_object_retention.assert_called_once_with(**target)

    s3.get_object_retention.side_effect = None
    with pytest.raises(AssertionError, match="did not reject"):
        require_rejected_null_protection_read(s3, target)
    s3.get_object_retention.side_effect = error("AccessDenied", 403)
    with pytest.raises(ClientError, match="AccessDenied"):
        require_rejected_null_protection_read(s3, target)
