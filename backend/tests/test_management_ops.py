import json
from types import SimpleNamespace

import pytest

from console import db, management_ops
from console.security import AppError


@pytest.fixture
def ledger(tmp_path):
    cfg = SimpleNamespace(database_path=tmp_path / "ops.db", data_dir=tmp_path)
    with db.connect(cfg) as conn:
        conn.execute("CREATE TABLE management_connections(id TEXT PRIMARY KEY,name TEXT,admin_url TEXT,s3_connection_id TEXT,protocol_baseline TEXT,endpoints_json TEXT,management_write_enabled INTEGER,permissions_json TEXT,created_at TEXT,updated_at TEXT)")
        conn.execute("INSERT INTO management_connections VALUES ('mg','test','http://admin',NULL,'4.48','{}',1,?, '', '')",
                     (json.dumps({'iam.manage': True, 'bucket.manage': True, 'bucket_write_prefixes': ['swc-']}),))
        management_ops.initialize(conn)
    return cfg, {"id": "mg", "management_write_enabled": True,
                 "permissions": {"iam.manage": True, "bucket.manage": True, "bucket_write_prefixes": ["swc-"]}}


def test_intent_is_durable_before_write_and_duplicate_never_reveals_secret(ledger):
    cfg, manager = ledger
    state = {"value": None, "calls": 0}

    def invoke():
        with db.connect(cfg) as conn:
            assert conn.execute("SELECT state FROM management_operations").fetchone()[0] == "dispatching"
        state.update(value={"access_key": "public-id", "secret_key": "generated-private-secret"}, calls=state["calls"] + 1)
        return state["value"]

    args = dict(settings=cfg, management=manager, actor_id="actor", action="iam.key.create", method="POST",
                path="/api/users/test/access-keys", payload={}, idempotency_key="same", invoke=invoke,
                readback=lambda: state["value"], verify=lambda before, after, response: after["access_key"] == response["access_key"],
                return_created_secret=True)
    first = management_ops.perform_operation(**args)
    assert first["status"] == "confirmed"
    assert first["created_credentials"]["secret_key"] == "generated-private-secret"
    repeat = management_ops.perform_operation(**args)
    assert state["calls"] == 1
    assert repeat["secret_receipt_available"] is False
    assert "created_credentials" not in repeat
    with db.connect(cfg) as conn:
        row = dict(conn.execute("SELECT * FROM management_operations").fetchone())
        assert "generated-private-secret" not in json.dumps(row)
        assert "public-id" in row["result_json"]


def test_write_success_but_readback_failure_is_uncertain_not_failed(ledger):
    cfg, manager = ledger
    state = {"written": False, "calls": 0}

    def invoke():
        state["written"] = True
        state["calls"] += 1
        return {"success": True}

    def readback():
        if state["written"]:
            raise AppError("ADMIN_ENDPOINT_NOT_FOUND", "not visible yet", 404)
        return {"name": "test"}

    with pytest.raises(AppError):
        management_ops.perform_operation(cfg, manager, "actor", "iam.user.update", "PUT", "/api/users/test", {},
                                         idempotency_key="uncertain-user-update",
                                         invoke=invoke, readback=readback, verify=lambda *args: True)
    with db.connect(cfg) as conn:
        assert conn.execute("SELECT state FROM management_operations").fetchone()[0] == "needs_review"
    replay = management_ops.perform_operation(cfg, manager, "actor", "iam.user.update", "PUT", "/api/users/test", {},
                                             idempotency_key="uncertain-user-update",
                                             invoke=invoke, readback=readback, verify=lambda *args: True)
    assert replay["replayed"] is True
    assert replay["status"] == "needs_review"
    assert state["calls"] == 1


def test_missing_idempotency_key_blocks_before_journal_and_dispatch(ledger):
    cfg, manager = ledger
    calls = []
    with pytest.raises(AppError) as error:
        management_ops.perform_operation(cfg, manager, "actor", "iam.user.create", "POST", "/api/users", {},
                                         invoke=lambda: calls.append(True))
    assert error.value.code == "IDEMPOTENCY_KEY_REQUIRED"
    assert error.value.status == 422
    assert calls == []
    with db.connect(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_operations").fetchone()[0] == 0


def test_same_idempotency_key_with_changed_request_returns_conflict(ledger):
    cfg, manager = ledger
    args = dict(settings=cfg, management=manager, actor_id="actor", action="iam.user.update",
                method="PUT", path="/api/users/test", payload={"email": "first@example.com"},
                idempotency_key="same-request", invoke=lambda: {"success": True},
                verify=lambda *args: False)
    management_ops.perform_operation(**args)
    args["payload"] = {"email": "second@example.com"}
    with pytest.raises(AppError) as error:
        management_ops.perform_operation(**args)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert error.value.status == 409


def test_permission_and_bucket_boundary_block_before_call(ledger):
    cfg, manager = ledger
    called = []
    manager["management_write_enabled"] = False
    with pytest.raises(AppError):
        management_ops.perform_operation(cfg, manager, "actor", "iam.user.create", "POST", "/api/users", {},
                                         idempotency_key="disabled-user", invoke=lambda: called.append(True))
    manager["management_write_enabled"] = True
    with pytest.raises(AppError):
        management_ops.perform_operation(cfg, manager, "actor", "bucket.create", "POST", "/api/s3/buckets", {"name": "production"},
                                         idempotency_key="forbidden-bucket", invoke=lambda: called.append(True))
    assert called == []


@pytest.mark.parametrize("secret", ["a-real-secret", "[REDACTED]"])
@pytest.mark.parametrize("field", ["secret_key", "aws_secret_access_key", "token", "secret"])
def test_manual_secret_input_never_journaled(ledger, secret, field):
    cfg, manager = ledger
    with pytest.raises(AppError):
        management_ops.perform_operation(cfg, manager, "actor", "iam.key.create", "POST", "/api/users/test/access-keys", {field: secret},
                                         idempotency_key=f"secret-{field}", invoke=lambda: {})
    with db.connect(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_operations").fetchone()[0] == 0


def test_unverifiable_write_does_not_claim_success(ledger):
    cfg, manager = ledger
    result = management_ops.perform_operation(cfg, manager, "actor", "iam.user.create", "POST", "/api/users", {},
                                              idempotency_key="unverified-user", invoke=lambda: {"success": True})
    assert result["status"] == "needs_review"


def test_policy_revoked_during_readback_prevents_dispatch(ledger):
    cfg, manager = ledger
    calls = []

    def readback():
        with db.connect(cfg) as conn:
            conn.execute("UPDATE management_connections SET management_write_enabled=0 WHERE id='mg'")
        return {"username": "test"}

    with pytest.raises(AppError) as error:
        management_ops.perform_operation(cfg, manager, "actor", "iam.user.update", "PUT", "/api/users/test", {},
                                         idempotency_key="revoked-user-update",
                                         readback=readback, invoke=lambda: calls.append(True), verify=lambda *args: True)
    assert error.value.code == "MANAGEMENT_WRITE_DISABLED"
    assert not calls
    with db.connect(cfg) as conn:
        assert conn.execute("SELECT state FROM management_operations").fetchone()[0] == "failed"


def test_missing_generated_secret_is_not_an_available_receipt(ledger):
    cfg, manager = ledger
    result = management_ops.perform_operation(cfg, manager, "actor", "iam.key.create", "POST", "/api/users/test/access-keys", {},
                                              idempotency_key="missing-secret-receipt",
                                              invoke=lambda: {"access_key": "public"}, verify=lambda *args: True,
                                              return_created_secret=True)
    assert result["status"] == "confirmed"
    assert result["secret_receipt_available"] is False
    assert "created_credentials" not in result
