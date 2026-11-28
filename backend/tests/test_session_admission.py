from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading

import pytest
from fastapi.testclient import TestClient

from console import core, db, login_admission
from console.config import Settings
from console.security import AppError


@pytest.fixture
def auth(tmp_path, monkeypatch):
    clock = {"seconds": 0}
    start = datetime(2026, 11, 28, tzinfo=timezone.utc)
    monkeypatch.setattr(core, "utc_now", lambda: core._session_timestamp(start + timedelta(seconds=clock["seconds"])))
    monkeypatch.setattr(login_admission, "time", SimpleNamespace(time=lambda: clock["seconds"]))
    monkeypatch.setattr(core, "hash_password", lambda password: "hash:" + password)
    monkeypatch.setattr(core, "verify_password", lambda password, stored: stored == "hash:" + password)
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "auth.sqlite", admin_password="password",
                        csrf_signing_key="test-csrf", cursor_signing_key="test-cursor", dev_insecure_cookie=True,
                        session_ttl_seconds=120, session_absolute_ttl_seconds=300,
                        allowed_origins=["http://testserver"])
    db.initialize(settings)
    with db.connect(settings) as conn:
        core.bootstrap_admin(conn, settings)
    return settings, clock


def test_existing_session_slides_with_coalesced_writes_and_absolute_cap(auth):
    settings, clock = auth
    with db.connect(settings) as conn:
        result = core.login(conn, settings, "admin", "password")
        baseline = conn.total_changes
        clock["seconds"] = 30
        _, early = core.current_user_from_token(conn, result["session_token"], settings)
        assert conn.total_changes == baseline
        assert early["expires_at"] == result["expires_at"]
        for second in (60, 120, 180, 240):
            clock["seconds"] = second
            _, session = core.current_user_from_token(conn, result["session_token"], settings)
            assert session["expires_at"] <= session["absolute_expires_at"]
        assert session["expires_at"] == result["absolute_expires_at"]
        assert conn.total_changes == baseline + 4
        clock["seconds"] = 300
        with pytest.raises(AppError, match="expired"):
            core.current_user_from_token(conn, result["session_token"], settings)


def test_login_prunes_only_expired_or_revoked_sessions(auth):
    settings, clock = auth
    with db.connect(settings) as conn:
        expired = core.login(conn, settings, "admin", "password")
        revoked = core.login(conn, settings, "admin", "password")
        active = core.login(conn, settings, "admin", "password")
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00.000000Z' WHERE id=?", (expired["session_id"],))
        core.logout(conn, revoked["session_id"])
        for result in (expired, revoked):
            with pytest.raises(AppError):
                core.current_user_from_token(conn, result["session_token"], settings)
        fresh = core.login(conn, settings, "admin", "password")
        ids = {row[0] for row in conn.execute("SELECT id FROM sessions")}
        assert ids == {active["session_id"], fresh["session_id"]}


def test_password_verification_runs_after_admission_commit_and_outside_write_transaction(auth, monkeypatch):
    settings, _clock = auth
    with login_admission.admit(settings, "peer"):
        with db.connect(settings) as conn:
            def verify(password, stored):
                assert not conn.in_transaction
                with db.connect(settings) as observer:
                    assert observer.execute("SELECT count(*) FROM login_admission_leases").fetchone()[0] == 1
                    observer.execute("BEGIN IMMEDIATE")
                return True

            monkeypatch.setattr(core, "verify_password", verify)
            assert core.login(conn, settings, "admin", "password")["session_token"]


def test_cookie_uses_absolute_lifetime_and_authenticated_responses_do_not_restore_it(auth):
    from console.main import create_app

    settings, clock = auth
    with TestClient(create_app(settings)) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "password"}, headers={"Origin": "http://testserver"})
        assert login.status_code == 200
        assert "Max-Age=300" in login.headers["set-cookie"]
        for second in (60, 120, 180, 240):
            clock["seconds"] = second
            response = client.get("/api/v1/auth/me")
            assert response.status_code == 200
            assert "set-cookie" not in response.headers
        csrf = response.json()["csrf_token"]
        response = client.post("/api/v1/auth/logout", headers={"Origin": "http://testserver", "X-CSRF-Token": csrf})
        assert response.status_code == 200
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.get("/api/v1/auth/me").status_code == 401


def test_initial_cookie_is_capped_when_absolute_ttl_is_shorter(auth):
    settings, _clock = auth
    with db.connect(settings) as conn:
        result = core.login(conn, replace(settings, session_absolute_ttl_seconds=30), "admin", "password")
    assert core.session_cookie_max_age(result) == 30


def test_idle_expiry_is_enforced_while_absolute_lifetime_cookie_still_exists(auth):
    from console.main import create_app

    settings, clock = auth
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "password"}, headers={"Origin": "http://testserver"})
        assert response.status_code == 200
        assert settings.session_cookie_name in client.cookies
        clock["seconds"] = 120
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


def test_relogin_revokes_previous_cookie_without_refreshing_it(auth):
    from console.main import create_app

    settings, _clock = auth
    with TestClient(create_app(settings)) as client:
        body = {"username": "admin", "password": "password"}
        for _ in range(2):
            old_token = client.cookies.get(settings.session_cookie_name)
            response = client.post("/api/v1/auth/login", json=body, headers={"Origin": "http://testserver"})
            assert response.status_code == 200
        new_token = client.cookies.get(settings.session_cookie_name)
        assert old_token != new_token
        response = client.get("/api/v1/auth/me", headers={"Cookie": f"{settings.session_cookie_name}={old_token}"})
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert client.get("/api/v1/auth/me").status_code == 200


def test_login_admission_is_bounded_before_verification_and_ignores_forwarded_peer(auth, monkeypatch):
    from console.main import create_app

    settings, clock = auth
    calls = []
    monkeypatch.setattr(core, "verify_password", lambda *_: calls.append(True) or False)
    with TestClient(create_app(settings)) as client:
        for attempt in range(login_admission.PEER_ATTEMPTS):
            response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"},
                                   headers={"Origin": "http://testserver", "X-Forwarded-For": str(attempt)})
            assert response.status_code == 401
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"}, headers={"Origin": "http://testserver"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.json()["error"]["code"] == "LOGIN_RATE_LIMITED"
        assert len(calls) == login_admission.PEER_ATTEMPTS
        clock["seconds"] = 60
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"}, headers={"Origin": "http://testserver"}).status_code == 401
    with db.connect(settings) as conn:
        assert conn.execute("SELECT count(*) FROM login_admission_leases").fetchone()[0] == 0
        rows = [tuple(row) for row in conn.execute("SELECT * FROM login_admission_windows")]
        assert len(rows) == 2
        assert "password" not in str(rows) and "wrong" not in str(rows) and "testclient" not in str(rows)


def test_concurrent_admission_and_abandoned_lease_recovery(auth):
    settings, clock = auth
    entered = threading.Barrier(3)
    release = threading.Event()

    def hold(peer):
        with login_admission.admit(settings, peer):
            entered.wait(timeout=5)
            assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(hold, f"peer-{index}") for index in range(2)]
        try:
            entered.wait(timeout=5)
            with pytest.raises(AppError) as error:
                with login_admission.admit(settings, "third"):
                    pytest.fail("third password verification was admitted")
            assert error.value.status == 429
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    with db.connect(settings) as conn:
        conn.executemany("INSERT INTO login_admission_leases VALUES(?,?)", [("crashed-1", 30), ("crashed-2", 30)])
    clock["seconds"] = 60
    with login_admission.admit(settings, "recovery"):
        pass
    with db.connect(settings) as conn:
        assert conn.execute("SELECT count(*) FROM login_admission_leases").fetchone()[0] == 0


def test_global_admission_caps_peer_churn_and_rejects_long_inputs_before_hash(auth, monkeypatch):
    settings, clock = auth
    for index in range(login_admission.GLOBAL_ATTEMPTS):
        with login_admission.admit(settings, f"peer-{index}"):
            pass
    with pytest.raises(AppError):
        with login_admission.admit(settings, "another-peer"):
            pytest.fail("global admission cap was bypassed")
    with db.connect(settings) as conn:
        assert conn.execute("SELECT count(*) FROM login_admission_windows").fetchone()[0] == login_admission.GLOBAL_ATTEMPTS + 1
    clock["seconds"] = 60
    with login_admission.admit(settings, "new-window"):
        pass
    monkeypatch.setattr(core, "verify_password", lambda *_: pytest.fail("long credentials reached password verification"))
    with db.connect(settings) as conn:
        for username, password in (("a" * 257, "password"), ("admin", "p" * 4097)):
            with pytest.raises(AppError):
                core.login(conn, settings, username, password)
