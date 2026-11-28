from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Event

import boto3
import pytest

from console import config, storage
from console.config import Settings


class FakeClient:
    def __init__(self, credentials):
        self.credentials = credentials
        self.calls = []
        self.closed = 0
        self.entered = Event()
        self.release = None
        self.failure = None

    def head_object(self, **kwargs):
        assert not self.closed
        self.calls.append(kwargs)
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        assert not self.closed, "an active HEAD must retain its client through eviction"
        if self.failure:
            raise self.failure
        return {"ETag": kwargs["Key"], "ContentLength": 12}

    def close(self):
        self.closed += 1


@pytest.fixture
def cached_storage(tmp_path, monkeypatch):
    storage.clear_head_client_cache()
    with config._SECRET_REGISTRY_LOCK:
        config._SECRET_REGISTRY_CACHE.clear()
    registry = tmp_path / "synthetic-registry.json"
    entry = {"access_key_id": "synthetic-access", "secret_access_key": "secret-a", "allowed_endpoint_url": "http://storage.invalid"}
    registry.write_text(json.dumps({"test": entry}), encoding="utf-8")
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "test.db", secrets_file=registry)
    connection = {"id": "connection-a", "secret_ref": "test", "endpoint_url": "http://storage.invalid", "region": "us-east-1", "verify_tls": True}
    clients = []

    def build(_service, **kwargs):
        owned = FakeClient(kwargs)
        clients.append(owned)
        return owned

    monkeypatch.setattr(boto3, "client", build)
    yield settings, connection, entry, clients
    storage.clear_head_client_cache()


def test_thumbnail_head_burst_reuses_build_and_parse_but_reads_current_registry(cached_storage, monkeypatch):
    settings, connection, _entry, clients = cached_storage
    counts = {"reads": 0, "parses": 0}
    read = Path.read_bytes
    loads = config.json.loads

    def read_bytes(path):
        if path == settings.secrets_file:
            counts["reads"] += 1
        return read(path)

    def parse(content, *args, **kwargs):
        counts["parses"] += 1
        return loads(content, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(config.json, "loads", parse)
    for index in range(60):
        assert storage.head_object(connection, settings, Bucket="synthetic", Key=f"photo-{index}")["ETag"] == f"photo-{index}"
    assert len(clients) == 1
    assert len(clients[0].calls) == 60
    assert counts == {"reads": 62, "parses": 1}
    assert clients[0].closed == 0


def test_registry_results_are_isolated_and_cache_is_bounded(cached_storage, monkeypatch):
    settings, _connection, _entry, _clients = cached_storage
    first = config.load_secret_registry(settings)
    first["test"]["secret_access_key"] = "caller-mutation"
    assert config.load_secret_registry(settings)["test"]["secret_access_key"] == "secret-a"
    monkeypatch.setattr(config, "_SECRET_REGISTRY_CACHE_LIMIT", 2)
    for index in range(4):
        path = settings.data_dir / f"synthetic-{index}.json"
        path.write_text(json.dumps({"secrets": {"sample": {"value": index}}}), encoding="utf-8")
        assert config.load_secret_registry(replace(settings, secrets_file=path)) == {"sample": {"value": index}}
    assert len(config._SECRET_REGISTRY_CACHE) == 2


def test_rotation_with_preserved_size_and_mtime_replaces_and_closes_idle_client(cached_storage):
    settings, connection, entry, clients = cached_storage
    storage.head_object(connection, settings, Bucket="synthetic", Key="old")
    metadata = settings.secrets_file.stat()
    entry["secret_access_key"] = "secret-b"
    settings.secrets_file.write_text(json.dumps({"test": entry}), encoding="utf-8")
    os.utime(settings.secrets_file, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert settings.secrets_file.stat().st_size == metadata.st_size
    storage.head_object(connection, settings, Bucket="synthetic", Key="new")
    assert len(clients) == 2
    assert clients[0].closed == 1
    assert clients[1].credentials["aws_secret_access_key"] == "secret-b"
    assert clients[0].calls == [{"Bucket": "synthetic", "Key": "old"}]


@pytest.mark.parametrize("change", ["region", "addressing_style", "verify_tls", "connect_timeout", "read_timeout", "session_token", "endpoint"])
def test_cached_client_configuration_isolation(cached_storage, change):
    settings, connection, entry, clients = cached_storage
    storage.head_object(connection, settings, Bucket="synthetic", Key="before")
    if change == "connect_timeout":
        settings = replace(settings, s3_connect_timeout_seconds=17)
    elif change == "read_timeout":
        settings = replace(settings, s3_read_timeout_seconds=19)
    elif change == "session_token":
        entry["session_token"] = "synthetic-session"
        settings.secrets_file.write_text(json.dumps({"test": entry}), encoding="utf-8")
    elif change == "endpoint":
        connection = {**connection, "endpoint_url": "http://replacement.invalid"}
        entry["allowed_endpoint_url"] = connection["endpoint_url"]
        settings.secrets_file.write_text(json.dumps({"test": entry}), encoding="utf-8")
    else:
        value = {"region": "another-region", "addressing_style": "virtual", "verify_tls": False}[change]
        connection = {**connection, change: value}
    storage.head_object(connection, settings, Bucket="synthetic", Key="after")
    assert len(clients) == 2
    assert clients[0].closed == 1
    current = clients[1].credentials
    assert current["endpoint_url"] == connection["endpoint_url"]
    assert current["region_name"] == connection["region"]
    assert current["verify"] is connection["verify_tls"]
    assert current["config"].connect_timeout == settings.s3_connect_timeout_seconds
    assert current["config"].read_timeout == settings.s3_read_timeout_seconds
    assert current["config"].s3["addressing_style"] == connection.get("addressing_style", "path")
    assert current.get("aws_session_token") == entry.get("session_token")


@pytest.mark.parametrize("replacement", ["{}", "{bad-json", "[]", None])
def test_registry_revoke_or_failure_blocks_reuse_and_closes_idle_client(cached_storage, replacement):
    settings, connection, _entry, clients = cached_storage
    storage.head_object(connection, settings, Bucket="synthetic", Key="before")
    if replacement is None:
        settings.secrets_file.unlink()
    else:
        settings.secrets_file.write_text(replacement, encoding="utf-8")
    with pytest.raises((storage.StorageError, ValueError, OSError)):
        storage.head_object(connection, settings, Bucket="synthetic", Key="must-not-dispatch")
    assert clients[0].closed == 1
    assert len(clients[0].calls) == 1
    assert not storage._HEAD_CLIENTS


@pytest.mark.parametrize("changed", [{"allowed_endpoint_url": ""}, {"allowed_endpoint_url": "http://other.invalid"}, {"allowed_connection_id": "another-connection"}])
def test_cached_client_rechecks_endpoint_and_connection_approval(cached_storage, changed):
    settings, connection, entry, clients = cached_storage
    storage.head_object(connection, settings, Bucket="synthetic", Key="before")
    entry.update(changed)
    settings.secrets_file.write_text(json.dumps({"test": entry}), encoding="utf-8")
    with pytest.raises(storage.StorageError):
        storage.head_object(connection, settings, Bucket="synthetic", Key="must-not-dispatch")
    assert len(clients) == 1
    assert clients[0].closed == 1
    assert len(clients[0].calls) == 1


def test_concurrent_heads_build_once_and_eviction_waits_for_active_reader(cached_storage, monkeypatch):
    settings, connection, _entry, clients = cached_storage
    monkeypatch.setattr(storage, "_HEAD_CLIENT_CACHE_LIMIT", 1)
    storage.head_object(connection, settings, Bucket="synthetic", Key="prime")
    release = Event()
    clients[0].entered.clear()
    clients[0].release = release
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(storage.head_object, connection, settings, Bucket="synthetic", Key=f"active-{index}") for index in range(6)]
        assert clients[0].entered.wait(5)
        try:
            # Each reader retains the same entry, even when another connection evicts it.
            with storage._HEAD_CLIENT_LOCK:
                active = next(iter(storage._HEAD_CLIENTS.values())).users
            assert active >= 1
            storage.head_object({**connection, "id": "connection-b"}, settings, Bucket="synthetic", Key="evict")
            assert clients[0].closed == 0
            assert len(storage._HEAD_CLIENTS) == 1
        finally:
            release.set()
        assert all(future.result()["ContentLength"] == 12 for future in pending)
    assert clients[0].closed == 1
    assert len(clients) <= 3


def test_concurrent_first_use_builds_only_one_client(cached_storage):
    settings, connection, _entry, clients = cached_storage
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(storage.head_object, connection, settings, Bucket="synthetic", Key=f"photo-{index}") for index in range(32)]
        assert all(future.result()["ContentLength"] == 12 for future in pending)
    assert len(clients) == 1
    assert len(clients[0].calls) == 32


def test_queued_cache_hit_checks_revocation_after_unrelated_construction(cached_storage, monkeypatch):
    settings, connection, _entry, clients = cached_storage
    storage.head_object(connection, settings, Bucket="synthetic", Key="before")
    build = boto3.client
    secret_for = storage.secret_for_connection
    constructing = Event()
    release = Event()
    queued = Event()
    checked = Event()

    def held_build(*args, **kwargs):
        constructing.set()
        assert release.wait(5)
        return build(*args, **kwargs)

    def observe_check(supplied_settings, supplied_connection):
        secret = secret_for(supplied_settings, supplied_connection)
        if supplied_connection["id"] == connection["id"]:
            checked.set()
        return secret

    def queued_head():
        queued.set()
        return storage.head_object(connection, settings, Bucket="synthetic", Key="revoked")

    monkeypatch.setattr(boto3, "client", held_build)
    monkeypatch.setattr(storage, "secret_for_connection", observe_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        other = pool.submit(storage.head_object, {**connection, "id": "connection-b"}, settings, Bucket="synthetic", Key="other")
        assert constructing.wait(5)
        pending = pool.submit(queued_head)
        assert queued.wait(5)
        try:
            # Give an incorrectly unlocked check time to capture the old credentials.
            checked.wait(0.2)
            settings.secrets_file.write_text("{}", encoding="utf-8")
        finally:
            release.set()
        with pytest.raises(storage.StorageError):
            other.result()
        with pytest.raises(storage.StorageError):
            pending.result()
    assert clients[0].calls == [{"Bucket": "synthetic", "Key": "before"}]
    assert clients[0].closed == 1


def test_rotation_during_construction_discards_old_client_before_any_dispatch(cached_storage, monkeypatch):
    settings, connection, entry, clients = cached_storage
    build = boto3.client

    def rotating_build(*args, **kwargs):
        owned = build(*args, **kwargs)
        if len(clients) == 1:
            entry["secret_access_key"] = "secret-b"
            settings.secrets_file.write_text(json.dumps({"test": entry}), encoding="utf-8")
        return owned

    monkeypatch.setattr(boto3, "client", rotating_build)
    storage.head_object(connection, settings, Bucket="synthetic", Key="after-rotation")
    assert len(clients) == 2
    assert clients[0].closed == 1
    assert clients[0].calls == []
    assert clients[1].credentials["aws_secret_access_key"] == "secret-b"


def test_head_failure_releases_lease_and_fresh_clients_keep_independent_ownership(cached_storage, monkeypatch):
    settings, connection, _entry, clients = cached_storage
    first = storage.client(connection, settings)
    second = storage.client(connection, settings)
    assert first is not second
    first.close()
    assert second.closed == 0
    storage.head_object(connection, settings, Bucket="synthetic", Key="cached")
    clients[-1].failure = RuntimeError("synthetic-head-failure")
    with pytest.raises(RuntimeError, match="synthetic-head-failure"):
        storage.head_object(connection, settings, Bucket="synthetic", Key="failed")
    assert next(iter(storage._HEAD_CLIENTS.values())).users == 0
    storage.clear_head_client_cache()
    assert clients[-1].closed == 1
    assert second.closed == 0
    second.close()
