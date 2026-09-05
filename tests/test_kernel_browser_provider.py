from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from ash.browser.providers.kernel import KernelBrowserProvider, _KernelRuntime


@pytest.mark.parametrize(
    "base_url",
    ["file:///tmp/kernel", "http://example.com", "https://user:pass@example.com"],
)
def test_kernel_provider_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="kernel_base_url_must_be_https_or_loopback"):
        KernelBrowserProvider(api_key="key", base_url=base_url, project_id=None)


class _Page:
    url = "https://example.com"

    async def goto(self, url, **kwargs):
        self.url = url

    async def title(self):
        return "Example"

    async def content(self):
        return "<html><body>Example body</body></html>"

    async def screenshot(self, **kwargs):
        return b"png"

    def locator(self, selector):
        return SimpleNamespace(first=_Locator(selector))


class _Locator:
    def __init__(self, selector):
        self.selector = selector

    async def inner_text(self, **kwargs):
        return "Example body"

    async def click(self):
        return None

    async def fill(self, text):
        return None

    async def press_sequentially(self, text):
        return None

    async def wait_for(self, **kwargs):
        return None


@pytest.mark.asyncio
async def test_kernel_provider_supports_page_actions() -> None:
    provider = KernelBrowserProvider(
        api_key="key", base_url="https://api.onkernel.com", project_id=None
    )
    provider._runtimes["kernel-1"] = _KernelRuntime(
        playwright=SimpleNamespace(),
        browser=SimpleNamespace(),
        page=_Page(),
    )

    goto = await provider.goto(
        provider_session_id="kernel-1",
        url="https://example.com/path",
        timeout_seconds=10,
    )
    extracted = await provider.extract(
        provider_session_id="kernel-1",
        html=None,
        mode="text",
        selector=None,
        max_chars=100,
    )
    screenshot = await provider.screenshot(provider_session_id="kernel-1")

    assert goto.url == "https://example.com/path"
    assert goto.title == "Example"
    assert extracted.data == {"text": "Example body"}
    assert screenshot.image_bytes == b"png"


@pytest.mark.asyncio
async def test_kernel_provider_uses_current_browser_endpoint(monkeypatch) -> None:
    provider = KernelBrowserProvider(
        api_key="key", base_url="https://api.onkernel.com", project_id=None
    )
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return {
            "session_id": "kernel-1",
            "cdp_ws_url": "wss://example.invalid/cdp",
            "browser_live_view_url": "https://example.invalid/live",
        }

    async def fake_connect(payload):
        return "kernel-1", _KernelRuntime(
            playwright=SimpleNamespace(),
            browser=SimpleNamespace(),
            page=_Page(),
        )

    monkeypatch.setattr(provider, "_blocking_request", fake_request)
    monkeypatch.setattr(provider, "_connect", fake_connect)

    result = await provider.start_session(
        session_id="ash-local-id", profile_name=None, scope_key="user-1"
    )

    assert result.provider_session_id == "kernel-1"
    assert calls[0][0:2] == ("POST", "/browsers")
    assert calls[0][2]["stealth"] is True


def test_kernel_provider_does_not_follow_authenticated_redirects() -> None:
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
    provider = KernelBrowserProvider(
        api_key="must-not-be-forwarded",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        project_id=None,
    )
    try:
        with pytest.raises(ValueError, match="kernel_http_307"):
            provider._blocking_request("POST", "/browsers", {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert paths == ["/browsers"]
