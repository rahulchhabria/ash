"""Shared RPC server lifecycle helpers for runtime entrypoints."""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ash.context_token import get_default_context_token_service
from ash.rpc import RPCServer

if TYPE_CHECKING:
    from ash.integrations.runtime import IntegrationContext, IntegrationRuntime


@asynccontextmanager
async def active_rpc_server(
    *,
    runtime: IntegrationRuntime,
    context: IntegrationContext,
    socket_path: Path,
    enabled: bool = True,
) -> AsyncIterator[RPCServer | None]:
    """Start/stop the runtime RPC server and register integration methods."""
    if not enabled:
        yield None
        return

    tcp_bind_host = os.environ.get("ASH_RPC_TCP_BIND_HOST", "").strip()
    server = RPCServer(
        socket_path,
        tcp_host=tcp_bind_host or None,
        tcp_port=0 if tcp_bind_host else None,
    )
    runtime.register_rpc_methods(server, context)

    async def ping(_params: dict) -> dict[str, str]:
        return {"status": "ok"}

    server.register("health.ping", ping)
    await server.start()

    try:
        await _grant_sandbox_socket_access(socket_path)
    except Exception:
        await server.stop()
        raise

    async def probe_sandbox_rpc() -> bool:
        executor = getattr(context.components, "sandbox_executor", None)
        if executor is None:
            return False
        code = (
            "import socket; socket.setdefaulttimeout(3); "
            "from ash_sandbox_cli.rpc import rpc_call; "
            "assert rpc_call('health.ping', max_retries=0) == {'status': 'ok'}; "
            "print('rpc-connected')"
        )
        token = get_default_context_token_service().issue(
            effective_user_id="__readiness__", ttl_seconds=30
        )
        result = await executor.execute(
            "python -c " + shlex.quote(code),
            timeout=8,
            reuse_container=False,
            environment={**context.sandbox_env, "ASH_CONTEXT_TOKEN": token},
        )
        return result.success and "rpc-connected" in result.stdout

    if hasattr(runtime, "register_health_probe"):
        runtime.register_health_probe("sandbox_rpc", probe_sandbox_rpc)

    # Spec contract: specs/rpc.md
    # Project runtime transport hints via integration-owned sandbox env instead
    # of process-global environment mutation.
    if server.tcp_port:
        docker_host_alias = (
            os.environ.get("ASH_RPC_DOCKER_HOST_ALIAS", "host.docker.internal")
            .strip()
            .lower()
        )
        context.sandbox_env["ASH_RPC_HOST"] = (
            docker_host_alias or "host.docker.internal"
        )
        context.sandbox_env["ASH_RPC_PORT"] = str(server.tcp_port)
    try:
        yield server
    finally:
        context.sandbox_env.pop("ASH_RPC_HOST", None)
        context.sandbox_env.pop("ASH_RPC_PORT", None)
        await server.stop()


async def _grant_sandbox_socket_access(socket_path: Path) -> None:
    """Grant only the explicitly configured mapped UID access (specs/rpc.md)."""
    value = os.environ.get("ASH_RPC_SANDBOX_HOST_UID", "").strip()
    if not value:
        return
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError("ASH_RPC_SANDBOX_HOST_UID must be a positive numeric host UID")
    uid = int(value)
    for path, permissions in ((socket_path.parent, "--x"), (socket_path, "rw-")):
        proc = await asyncio.create_subprocess_exec(
            "setfacl",
            "-m",
            f"u:{uid}:{permissions}",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(
                f"Sandbox RPC ACL setup failed: {stderr.decode().strip()}"
            )
