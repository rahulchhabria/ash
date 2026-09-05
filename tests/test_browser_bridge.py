from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ash.browser.bridge import BrowserExecBridge, request_bridge_exec
from ash.context_token import ContextTokenService
from ash.sandbox.executor import ExecutionResult


@pytest.mark.parametrize(
    "base_url",
    ["https://example.com", "file:///tmp/socket", "http://user:pass@127.0.0.1:80"],
)
def test_browser_exec_bridge_rejects_non_loopback_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="bridge_base_url_must_be_loopback"):
        request_bridge_exec(
            base_url=base_url,
            token="secret",
            command="echo hi",
            timeout_seconds=5,
        )


def test_browser_exec_bridge_requires_auth() -> None:
    bridge = BrowserExecBridge.start(
        executor=lambda command, timeout_seconds, environment: ExecutionResult(
            exit_code=0,
            stdout=f"{command}:{timeout_seconds}:{len(environment)}",
            stderr="",
        )
    )
    try:
        with pytest.raises(ValueError, match="bridge_unauthorized"):
            request_bridge_exec(
                base_url=bridge.base_url,
                token="wrong-token",
                command="echo hi",
                timeout_seconds=5,
            )
    finally:
        bridge.stop()


def test_browser_exec_bridge_executes_with_valid_token() -> None:
    bridge = BrowserExecBridge.start(
        executor=lambda command, timeout_seconds, environment: ExecutionResult(
            exit_code=0,
            stdout=f"{command}:{timeout_seconds}:{environment.get('A', '')}",
            stderr="",
        )
    )
    try:
        result = request_bridge_exec(
            base_url=bridge.base_url,
            token=bridge.token,
            command="echo hi",
            timeout_seconds=5,
            environment={"A": "B"},
        )
        assert result.success
        assert result.stdout == "echo hi:5:B"
    finally:
        bridge.stop()


def test_browser_exec_bridge_rejects_token_with_wrong_scope_claim() -> None:
    service = ContextTokenService(secret=b"0123456789abcdef0123456789abcdef")
    bridge = BrowserExecBridge.start(
        executor=lambda command, timeout_seconds, environment: ExecutionResult(
            exit_code=0,
            stdout=f"{command}:{timeout_seconds}:{environment.get('A', '')}",
            stderr="",
        ),
        token_service=service,
        scope_key="scope-a",
        target="container-a",
    )
    wrong_scope_token = service.issue(
        effective_user_id="browser-bridge",
        provider="browser-bridge",
        session_key="scope-b",
        thread_id="container-a",
        ttl_seconds=120,
    )
    try:
        with pytest.raises(ValueError, match="bridge_unauthorized"):
            request_bridge_exec(
                base_url=bridge.base_url,
                token=wrong_scope_token,
                command="echo hi",
                timeout_seconds=5,
                environment={"A": "B"},
            )
    finally:
        bridge.stop()


def test_browser_exec_bridge_issue_token_refreshes_expired_tokens() -> None:
    service = ContextTokenService(
        secret=b"0123456789abcdef0123456789abcdef",
        leeway_seconds=0,
    )
    bridge = BrowserExecBridge.start(
        executor=lambda command, timeout_seconds, environment: ExecutionResult(
            exit_code=0,
            stdout=f"{command}:{timeout_seconds}:{environment.get('A', '')}",
            stderr="",
        ),
        token_service=service,
        token_ttl_seconds=10,
    )
    stale_token = bridge.issue_token(ttl_seconds=1)
    try:
        time.sleep(2.2)
        with pytest.raises(ValueError, match="bridge_unauthorized"):
            request_bridge_exec(
                base_url=bridge.base_url,
                token=stale_token,
                command="echo hi",
                timeout_seconds=5,
                environment={"A": "B"},
            )

        result = request_bridge_exec(
            base_url=bridge.base_url,
            token=bridge.issue_token(ttl_seconds=10),
            command="echo hi",
            timeout_seconds=5,
            environment={"A": "B"},
        )
        assert result.success
        assert result.stdout == "echo hi:5:B"
    finally:
        bridge.stop()


def test_browser_exec_bridge_does_not_follow_authenticated_redirects() -> None:
    paths: list[str] = []

    class _RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            paths.append(self.path)
            self.send_response(307)
            self.send_header("Location", "/capture")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            _ = (format, args)

    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ValueError, match="bridge_http_error:307"):
            request_bridge_exec(
                base_url=f"http://127.0.0.1:{server.server_address[1]}",
                token="must-not-be-forwarded",
                command="echo hi",
                timeout_seconds=5,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert paths == ["/exec"]
