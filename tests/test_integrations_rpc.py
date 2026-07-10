from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ash.config import AshConfig
from ash.config.models import ModelConfig
from ash.integrations import IntegrationRuntime, create_default_integrations
from ash.integrations.rpc import active_rpc_server
from ash.integrations.runtime import IntegrationContext


class _FakeRPCServer:
    def __init__(
        self,
        socket_path: Path,
        *,
        tcp_host: str | None = None,
        tcp_port: int = 43210,
        events: list[str] | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.tcp_bind_host = tcp_host
        self.tcp_host = "127.0.0.1"
        self.tcp_port = tcp_port
        self.methods: dict[str, Any] = {}
        self._events = events
        self._calls = calls
        if self._events is not None:
            self._events.append("init")

    def register(self, method: str, handler: Any) -> None:
        if self._calls is not None:
            self._calls.append(method)
        self.methods[method] = handler

    async def start(self) -> None:
        if self._events is not None:
            self._events.append("start")

    async def stop(self) -> None:
        if self._events is not None:
            self._events.append("stop")


@pytest.mark.asyncio
async def test_active_rpc_server_starts_and_stops(monkeypatch) -> None:
    events: list[str] = []

    runtime = cast(
        Any,
        SimpleNamespace(
            register_rpc_methods=lambda _server, _context: events.append("register")
        ),
    )
    context = cast(Any, SimpleNamespace(sandbox_env={}))

    monkeypatch.setattr(
        "ash.integrations.rpc.RPCServer",
        lambda socket_path, *, tcp_host=None: _FakeRPCServer(
            socket_path, tcp_host=tcp_host, events=events
        ),
    )

    async with active_rpc_server(
        runtime=runtime,
        context=context,
        socket_path=Path("rpc.sock"),
    ) as server:
        assert server is not None
        assert server.socket_path == Path("rpc.sock")
        assert server.tcp_bind_host == "0.0.0.0"  # noqa: S104
        assert context.sandbox_env["ASH_RPC_HOST"] == "host.docker.internal"
        assert context.sandbox_env["ASH_RPC_PORT"] == "43210"
        events.append("inside")

    assert events == ["init", "register", "start", "inside", "stop"]
    assert "ASH_RPC_HOST" not in context.sandbox_env
    assert "ASH_RPC_PORT" not in context.sandbox_env


@pytest.mark.asyncio
async def test_active_rpc_server_noops_when_disabled(monkeypatch) -> None:
    class _FakeRPCServer:
        def __init__(self, socket_path: Path) -> None:
            raise AssertionError("RPC server should not be constructed")

    runtime = cast(
        Any,
        SimpleNamespace(
            register_rpc_methods=lambda _server, _context: None,
        ),
    )
    context = cast(Any, SimpleNamespace(sandbox_env={}))

    monkeypatch.setattr("ash.integrations.rpc.RPCServer", _FakeRPCServer)

    async with active_rpc_server(
        runtime=runtime,
        context=context,
        socket_path=Path("rpc.sock"),
        enabled=False,
    ) as server:
        assert server is None


@pytest.mark.asyncio
async def test_active_rpc_server_uses_docker_host_alias_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = cast(
        Any,
        SimpleNamespace(register_rpc_methods=lambda _server, _context: None),
    )
    context = cast(Any, SimpleNamespace(sandbox_env={}))
    monkeypatch.setenv("ASH_RPC_DOCKER_HOST_ALIAS", "host.containers.internal")
    monkeypatch.setattr(
        "ash.integrations.rpc.RPCServer",
        lambda socket_path, *, tcp_host=None: _FakeRPCServer(
            socket_path, tcp_host=tcp_host, tcp_port=40123
        ),
    )

    async with active_rpc_server(
        runtime=runtime,
        context=context,
        socket_path=Path("rpc.sock"),
    ):
        assert context.sandbox_env["ASH_RPC_HOST"] == "host.containers.internal"
        assert context.sandbox_env["ASH_RPC_PORT"] == "40123"


@pytest.mark.asyncio
async def test_active_rpc_server_uses_tcp_bind_host_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = cast(
        Any,
        SimpleNamespace(register_rpc_methods=lambda _server, _context: None),
    )
    context = cast(Any, SimpleNamespace(sandbox_env={}))
    monkeypatch.setenv("ASH_RPC_TCP_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(
        "ash.integrations.rpc.RPCServer",
        lambda socket_path, *, tcp_host=None: _FakeRPCServer(
            socket_path, tcp_host=tcp_host, tcp_port=40123
        ),
    )

    async with active_rpc_server(
        runtime=runtime,
        context=context,
        socket_path=Path("rpc.sock"),
    ) as server:
        assert server is not None
        assert server.tcp_bind_host == "127.0.0.1"


@pytest.mark.asyncio
async def test_todo_rpc_methods_not_registered_when_todo_disabled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AshConfig(
        workspace=tmp_path / "workspace",
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    components = cast(
        Any,
        SimpleNamespace(
            memory_manager=None,
            agent=cast(
                Any, SimpleNamespace(install_integration_hooks=lambda **_: None)
            ),
        ),
    )
    runtime = IntegrationRuntime(
        create_default_integrations(mode="chat", include_todo=False).contributors
    )
    context = IntegrationContext(config=config, components=components, mode="chat")
    await runtime.setup(context)

    calls: list[str] = []
    monkeypatch.setattr(
        "ash.integrations.rpc.RPCServer",
        lambda socket_path, *, tcp_host=None: _FakeRPCServer(
            socket_path, tcp_host=tcp_host, tcp_port=41111, calls=calls
        ),
    )

    async with active_rpc_server(
        runtime=runtime,
        context=context,
        socket_path=tmp_path / "rpc.sock",
    ) as server:
        assert server is not None
        methods = cast(Any, server).methods
        assert not any(method.startswith("todo.") for method in methods)

    assert not any(method.startswith("todo.") for method in calls)
