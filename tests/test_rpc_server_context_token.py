from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any, cast

import pytest

from ash.context_token import ContextTokenService
from ash.rpc.server import RPCServer


def _service() -> ContextTokenService:
    return ContextTokenService(secret=b"test-secret-key-32-bytes-minimum")


@pytest.mark.asyncio
async def test_rpc_server_requires_context_token(tmp_path: Path) -> None:
    server = RPCServer(tmp_path / "rpc.sock", context_token_service=_service())

    async def _handler(params: dict[str, Any]) -> dict[str, Any]:
        return params

    server.register("echo", _handler)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "echo",
        "params": {},
    }
    response = await server._process_request(json.dumps(payload).encode("utf-8"))

    assert response.error is not None
    assert response.error.code == -32602
    assert "context token" in response.error.message.lower()


@pytest.mark.asyncio
async def test_rpc_server_uses_verified_identity_claims(tmp_path: Path) -> None:
    service = _service()
    server = RPCServer(tmp_path / "rpc.sock", context_token_service=service)

    async def _handler(params: dict[str, Any]) -> dict[str, Any]:
        return params

    server.register("echo", _handler)

    token = service.issue(
        effective_user_id="user-1",
        chat_id="chat-1",
        chat_type="private",
        provider="telegram",
        session_key="telegram_chat-1_user-1",
        thread_id="thread-1",
        source_username="alice",
        source_display_name="Alice",
    )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "echo",
        "params": {
            "context_token": token,
            "user_id": "attacker",
            "chat_id": "other-chat",
            "provider": "spoofed",
        },
    }
    response = await server._process_request(json.dumps(payload).encode("utf-8"))

    assert response.error is None
    assert response.result is not None
    assert response.result["user_id"] == "user-1"
    assert response.result["chat_id"] == "chat-1"
    assert response.result["provider"] == "telegram"
    assert response.result["thread_id"] == "thread-1"
    assert response.result["source_username"] == "alice"
    assert response.result["source_display_name"] == "Alice"


@pytest.mark.asyncio
async def test_rpc_server_keeps_browser_provider_param(tmp_path: Path) -> None:
    service = _service()
    server = RPCServer(tmp_path / "rpc.sock", context_token_service=service)

    async def _handler(params: dict[str, Any]) -> dict[str, Any]:
        return params

    server.register("browser.session.list", _handler)

    token = service.issue(
        effective_user_id="user-1",
        provider="telegram",
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "browser.session.list",
        "params": {
            "context_token": token,
            "provider": "sandbox",
        },
    }
    response = await server._process_request(json.dumps(payload).encode("utf-8"))

    assert response.error is None
    assert response.result is not None
    assert response.result["user_id"] == "user-1"
    assert response.result["provider"] == "sandbox"


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


@pytest.mark.asyncio
async def test_rpc_server_defaults_to_private_unix_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "run" / "rpc.sock"
    server = RPCServer(socket_path, context_token_service=_service())
    await server.start()
    try:
        assert server.tcp_host is None
        assert server.tcp_port is None
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rpc_internal_errors_do_not_leak_exception_details(
    tmp_path: Path,
) -> None:
    service = _service()
    server = RPCServer(tmp_path / "rpc.sock", context_token_service=service)

    async def _handler(_params: dict[str, Any]) -> None:
        raise RuntimeError("database password was exposed")

    server.register("fail", _handler)
    token = service.issue(effective_user_id="user-1")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "fail",
        "params": {"context_token": token},
    }
    response = await server._process_request(json.dumps(payload).encode("utf-8"))

    assert response.error is not None
    assert response.error.code == -32603
    assert response.error.message == "Internal server error"
    assert "password" not in response.error.message


@pytest.mark.asyncio
async def test_rpc_server_rejects_connections_over_limit(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_read(_reader: asyncio.StreamReader) -> None:
        started.set()
        await release.wait()
        return None

    monkeypatch.setattr("ash.rpc.server.read_message", fake_read)
    server = RPCServer(Path("rpc.sock"), read_timeout=1, max_connections=1)
    server._running = True
    first_writer = _FakeWriter()
    first = asyncio.create_task(
        server._handle_connection(asyncio.StreamReader(), cast(Any, first_writer))
    )
    await started.wait()

    second_writer = _FakeWriter()
    await server._handle_connection(
        asyncio.StreamReader(),
        cast(Any, second_writer),
    )

    assert second_writer.closed is True
    assert server._active_connections == 1
    await first
    assert server._active_connections == 0
    release.set()


@pytest.mark.asyncio
async def test_rpc_server_closes_idle_connections_after_timeout(monkeypatch) -> None:
    async def never_read(_reader: asyncio.StreamReader) -> None:
        await asyncio.Event().wait()
        return None

    monkeypatch.setattr("ash.rpc.server.read_message", never_read)
    server = RPCServer(Path("rpc.sock"), read_timeout=0.01)
    server._running = True
    writer = _FakeWriter()

    await server._handle_connection(asyncio.StreamReader(), cast(Any, writer))

    assert writer.closed is True
    assert server._active_connections == 0
