"""Tests for OAuth credential storage."""

import json
import stat

import pytest

from ash.auth.storage import AuthStorage, OAuthCredentials


class TestAuthStorage:
    def _make_storage(self, tmp_path):
        path = tmp_path / "auth.json"
        return AuthStorage(path=path)

    def test_save_and_load(self, tmp_path):
        storage = self._make_storage(tmp_path)
        creds = OAuthCredentials(
            access="access-token",
            refresh="refresh-token",
            expires=1700000000.0,
            account_id="acct_123",
        )
        storage.save("openai-oauth", creds)
        loaded = storage.load("openai-oauth")
        assert loaded is not None
        assert loaded.access == "access-token"
        assert loaded.refresh == "refresh-token"
        assert loaded.expires == 1700000000.0
        assert loaded.account_id == "acct_123"

    def test_load_nonexistent(self, tmp_path):
        storage = self._make_storage(tmp_path)
        assert storage.load("openai-oauth") is None

    def test_remove(self, tmp_path):
        storage = self._make_storage(tmp_path)
        creds = OAuthCredentials(access="a", refresh="r", expires=0.0, account_id="x")
        storage.save("openai-oauth", creds)
        assert storage.remove("openai-oauth") is True
        assert storage.load("openai-oauth") is None

    def test_remove_nonexistent(self, tmp_path):
        storage = self._make_storage(tmp_path)
        assert storage.remove("openai-oauth") is False

    def test_list_providers(self, tmp_path):
        storage = self._make_storage(tmp_path)
        assert storage.list_providers() == []

        creds = OAuthCredentials(access="a", refresh="r", expires=0.0, account_id="x")
        storage.save("provider-a", creds)
        storage.save("provider-b", creds)
        providers = storage.list_providers()
        assert sorted(providers) == ["provider-a", "provider-b"]

    def test_file_permissions(self, tmp_path):
        storage = self._make_storage(tmp_path)
        creds = OAuthCredentials(access="a", refresh="r", expires=0.0, account_id="x")
        storage.save("test", creds)
        file_stat = storage.path.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        assert mode == 0o600

    def test_parent_and_lock_permissions(self, tmp_path):
        storage = self._make_storage(tmp_path)
        creds = OAuthCredentials(access="a", refresh="r", expires=0.0, account_id="x")
        storage.save("test", creds)

        assert stat.S_IMODE(storage.path.parent.stat().st_mode) == 0o700
        lock_path = storage.path.with_name("auth.json.lock")
        with storage._lock:
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

    def test_failed_atomic_replace_preserves_existing_file(self, tmp_path, monkeypatch):
        storage = self._make_storage(tmp_path)
        old = OAuthCredentials(access="old", refresh="r", expires=0.0, account_id="x")
        new = OAuthCredentials(access="new", refresh="r", expires=0.0, account_id="x")
        storage.save("test", old)
        original = storage.path.read_bytes()

        def fail_replace(_source, _target):
            raise OSError("simulated replace failure")

        monkeypatch.setattr("ash.auth.storage.os.replace", fail_replace)
        with pytest.raises(OSError, match="simulated replace failure"):
            storage.save("test", new)

        assert storage.path.read_bytes() == original

    def test_multiple_providers(self, tmp_path):
        storage = self._make_storage(tmp_path)
        creds_a = OAuthCredentials(
            access="a1", refresh="r1", expires=1.0, account_id="id1"
        )
        creds_b = OAuthCredentials(
            access="a2", refresh="r2", expires=2.0, account_id="id2"
        )
        storage.save("provider-a", creds_a)
        storage.save("provider-b", creds_b)

        loaded_a = storage.load("provider-a")
        loaded_b = storage.load("provider-b")
        assert loaded_a is not None
        assert loaded_a.access == "a1"
        assert loaded_b is not None
        assert loaded_b.access == "a2"

    def test_overwrite_existing(self, tmp_path):
        storage = self._make_storage(tmp_path)
        creds1 = OAuthCredentials(
            access="old", refresh="r", expires=0.0, account_id="x"
        )
        creds2 = OAuthCredentials(
            access="new", refresh="r", expires=0.0, account_id="x"
        )
        storage.save("test", creds1)
        storage.save("test", creds2)
        loaded = storage.load("test")
        assert loaded is not None
        assert loaded.access == "new"

    def test_load_corrupt_file(self, tmp_path):
        storage = self._make_storage(tmp_path)
        storage.path.write_text("not valid json")
        assert storage.load("test") is None

    def test_load_missing_fields(self, tmp_path):
        storage = self._make_storage(tmp_path)
        storage.path.write_text(json.dumps({"test": {"access": "a"}}))
        assert storage.load("test") is None
