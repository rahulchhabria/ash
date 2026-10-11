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
        tcp_port: int | None = None,
        events: list[str] | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.tcp_bind_host = tcp_host
        self.tcp_host = tcp_host
        self.tcp_port = 43210 if tcp_host is not None and tcp_port == 0 else tcp_port
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
        lambda socket_path, *, tcp_host=None, tcp_port=None: _FakeRPCServer(
            socket_path,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            events=events,
        ),
    )

    async with active_rpc_server(
        runtime=runtime,
        context=context,
        socket_path=Path("rpc.sock"),
    ) as server:
        assert server is not None
        assert server.socket_path == Path("rpc.sock")
        assert server.tcp_bind_host is None
        assert "ASH_RPC_HOST" not in context.sandbox_env
        assert "ASH_RPC_PORT" not in context.sandbox_env
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
    monkeypatch.setenv("ASH_RPC_TCP_BIND_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("ASH_RPC_DOCKER_HOST_ALIAS", "host.containers.internal")
    monkeypatch.setattr(
        "ash.integrations.rpc.RPCServer",
        lambda socket_path, *, tcp_host=None, tcp_port=None: _FakeRPCServer(
            socket_path, tcp_host=tcp_host, tcp_port=tcp_port
        ),
    )

    async with active_rpc_server(
        runtime=runtime,
        context=context,
        socket_path=Path("rpc.sock"),
    ):
        assert context.sandbox_env["ASH_RPC_HOST"] == "host.containers.internal"
        assert context.sandbox_env["ASH_RPC_PORT"] == "43210"


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
        lambda socket_path, *, tcp_host=None, tcp_port=None: _FakeRPCServer(
            socket_path, tcp_host=tcp_host, tcp_port=tcp_port
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
        lambda socket_path, *, tcp_host=None, tcp_port=None: _FakeRPCServer(
            socket_path,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            calls=calls,
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


@pytest.mark.asyncio
async def test_mapped_uid_acl_is_narrow_and_reapplied(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from ash.integrations.rpc import _grant_sandbox_socket_access

    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(b"", b""))
        )

    monkeypatch.setenv("ASH_RPC_SANDBOX_HOST_UID", "100999")
    monkeypatch.setattr("ash.integrations.rpc.asyncio.create_subprocess_exec", spawn)
    path = tmp_path / "rpc.sock"
    await _grant_sandbox_socket_access(path)
    await _grant_sandbox_socket_access(path)
    expected = [
        ("setfacl", "-m", "u:100999:--x", str(tmp_path)),
        ("setfacl", "-m", "u:100999:rw-", str(path)),
    ]
    assert calls == expected + expected


@pytest.mark.asyncio
async def test_readiness_reports_dependency_failure():
    import httpx
    from fastapi import FastAPI

    from ash.server.routes.health import router

    runtime = IntegrationRuntime([])

    async def broken():
        return False

    runtime.register_health_probe("sandbox_rpc", broken)
    app = FastAPI()
    app.state.integration_runtime = runtime
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["sandbox_rpc"] is False

        async def working():
            return True

        runtime.register_health_probe("sandbox_rpc", working)
        assert (await client.get("/ready")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport", ["unix", "tcp_missing_unix", "tcp_refused_unix", "broken"]
)
async def test_readiness_uses_authenticated_client_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    import asyncio
    import os
    import shlex
    import socket

    from ash.sandbox.executor import ExecutionResult

    monkeypatch.delenv("ASH_RPC_SANDBOX_HOST_UID", raising=False)
    monkeypatch.delenv("ASH_RPC_TCP_BIND_HOST", raising=False)
    if transport.startswith("tcp"):
        monkeypatch.setenv("ASH_RPC_TCP_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("ASH_RPC_DOCKER_HOST_ALIAS", "127.0.0.1")
    path = tmp_path / "server" / "rpc.sock"
    client_path = path if transport == "unix" else tmp_path / "missing.sock"
    if transport == "tcp_refused_unix":
        # A stale socket exists, but no process is listening to it.
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(client_path))

    class LocalExecutor:
        async def execute(
            self,
            command: str,
            *,
            timeout: int,  # noqa: ASYNC109 - executor interface
            reuse_container: bool,
            environment: dict[str, str],
        ) -> ExecutionResult:
            assert reuse_container is False
            env = {k: v for k, v in os.environ.items() if not k.startswith("ASH_RPC_")}
            env.update(environment)
            env["ASH_RPC_SOCKET"] = str(client_path)
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecutionResult(
                proc.returncode or 0, stdout.decode(), stderr.decode()
            )

    runtime = IntegrationRuntime([])
    context = IntegrationContext(
        config=AshConfig(
            workspace=tmp_path,
            models={"default": ModelConfig(provider="openai", model="test")},
        ),
        components=cast(Any, SimpleNamespace(sandbox_executor=LocalExecutor())),
        mode="serve",
    )
    async with active_rpc_server(runtime=runtime, context=context, socket_path=path):
        checks = await runtime.check_health()
        assert checks["sandbox_rpc"] is (transport != "broken")
