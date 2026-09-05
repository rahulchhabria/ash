"""Kernel remote browser provider backed by Playwright over CDP."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ash.browser.providers.base import (
    ProviderExtractResult,
    ProviderGotoResult,
    ProviderScreenshotResult,
    ProviderStartResult,
)

logger = logging.getLogger(__name__)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass(slots=True)
class _KernelRuntime:
    playwright: Any
    browser: Any
    page: Any


class KernelBrowserProvider:
    """Drive Kernel cloud browsers through their returned CDP websocket URL."""

    name = "kernel"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        project_id: str | None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        normalized_base_url = base_url.rstrip("/")
        parsed_base_url = urlparse(normalized_base_url)
        is_loopback = parsed_base_url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not parsed_base_url.hostname
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.scheme not in {"http", "https"}
            or (parsed_base_url.scheme == "http" and not is_loopback)
        ):
            raise ValueError("kernel_base_url_must_be_https_or_loopback")
        self._base_url = normalized_base_url
        self._project_id = project_id
        self._runtimes: dict[str, _KernelRuntime] = {}
        self._runtime_lock = asyncio.Lock()

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ValueError("kernel_api_key_missing")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._project_id:
            headers["X-Project-Id"] = self._project_id
        return headers

    def _blocking_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(  # noqa: S310 - base URL is administrator configured.
            f"{self._base_url}{path}",
            method=method,
            data=body,
            headers=self._auth_headers(),
        )
        try:
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=20) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ValueError(f"kernel_http_{exc.code}:{detail}") from None
        except (URLError, TimeoutError) as exc:
            raise ValueError(f"kernel_request_failed:{exc}") from exc
        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("kernel_invalid_json") from exc
        if not isinstance(result, dict):
            raise ValueError("kernel_invalid_response")
        return result

    async def _connect(self, payload: dict[str, Any]) -> tuple[str, _KernelRuntime]:
        provider_session_id = str(payload.get("session_id") or "").strip()
        cdp_ws_url = str(payload.get("cdp_ws_url") or "").strip()
        if not provider_session_id or not cdp_ws_url:
            raise ValueError("kernel_session_response_missing_connection")

        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_ws_url)
            context = (
                browser.contexts[0] if browser.contexts else await browser.new_context()
            )
            page = context.pages[0] if context.pages else await context.new_page()
        except Exception:
            await playwright.stop()
            raise
        return provider_session_id, _KernelRuntime(playwright, browser, page)

    async def _runtime(self, provider_session_id: str | None) -> _KernelRuntime:
        if not provider_session_id:
            raise ValueError("session_not_found")
        existing = self._runtimes.get(provider_session_id)
        if existing is not None:
            return existing
        async with self._runtime_lock:
            existing = self._runtimes.get(provider_session_id)
            if existing is not None:
                return existing
            payload = await asyncio.to_thread(
                self._blocking_request,
                "GET",
                f"/browsers/{provider_session_id}",
            )
            resolved_id, runtime = await self._connect(payload)
            self._runtimes[resolved_id] = runtime
            return runtime

    async def start_session(
        self,
        *,
        session_id: str,
        profile_name: str | None,
        scope_key: str | None = None,
    ) -> ProviderStartResult:
        _ = (session_id, scope_key)
        payload: dict[str, Any] = {
            "headless": False,
            "stealth": True,
            "timeout_seconds": 900,
        }
        if profile_name:
            payload["profile"] = {"name": profile_name, "save_changes": True}
        created = await asyncio.to_thread(
            self._blocking_request,
            "POST",
            "/browsers",
            payload,
        )
        try:
            provider_session_id, runtime = await self._connect(created)
        except Exception:
            created_id = str(created.get("session_id") or "").strip()
            if created_id:
                try:
                    await asyncio.to_thread(
                        self._blocking_request,
                        "DELETE",
                        f"/browsers/{created_id}",
                    )
                except Exception as cleanup_error:
                    logger.debug(
                        "kernel_session_cleanup_failed",
                        extra={"error.message": str(cleanup_error)},
                    )
            raise
        self._runtimes[provider_session_id] = runtime
        return ProviderStartResult(
            provider_session_id=provider_session_id,
            metadata={
                "engine": "playwright",
                "remote": True,
                "live_view_available": bool(created.get("browser_live_view_url")),
            },
        )

    async def close_session(self, *, provider_session_id: str | None) -> None:
        if not provider_session_id:
            return
        runtime = self._runtimes.pop(provider_session_id, None)
        if runtime is not None:
            try:
                await runtime.browser.close()
            finally:
                await runtime.playwright.stop()
        try:
            await asyncio.to_thread(
                self._blocking_request,
                "DELETE",
                f"/browsers/{provider_session_id}",
            )
        except Exception:
            return

    async def shutdown(self) -> None:
        for session_id in list(self._runtimes):
            await self.close_session(provider_session_id=session_id)

    async def goto(
        self,
        *,
        provider_session_id: str | None,
        url: str,
        timeout_seconds: float,
    ) -> ProviderGotoResult:
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError("invalid_url_scheme")
        runtime = await self._runtime(provider_session_id)
        await runtime.page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(max(1, timeout_seconds) * 1000),
        )
        return ProviderGotoResult(
            url=runtime.page.url,
            title=await runtime.page.title(),
            html=await runtime.page.content(),
        )

    async def extract(
        self,
        *,
        provider_session_id: str | None,
        html: str | None,
        mode: str,
        selector: str | None,
        max_chars: int,
    ) -> ProviderExtractResult:
        _ = html
        runtime = await self._runtime(provider_session_id)
        if mode == "title":
            return ProviderExtractResult(
                data={"title": (await runtime.page.title())[:max_chars]}
            )
        if mode != "text":
            raise ValueError("unsupported_extract_mode")
        locator = runtime.page.locator(selector or "body").first
        text = await locator.inner_text(timeout=5000)
        return ProviderExtractResult(data={"text": text[:max_chars]})

    async def click(
        self,
        *,
        provider_session_id: str | None,
        selector: str,
    ) -> None:
        runtime = await self._runtime(provider_session_id)
        await runtime.page.locator(selector).first.click()

    async def type(
        self,
        *,
        provider_session_id: str | None,
        selector: str,
        text: str,
        clear_first: bool,
    ) -> None:
        runtime = await self._runtime(provider_session_id)
        locator = runtime.page.locator(selector).first
        if clear_first:
            await locator.fill(text)
        else:
            await locator.press_sequentially(text)

    async def wait_for(
        self,
        *,
        provider_session_id: str | None,
        selector: str,
        timeout_seconds: float,
    ) -> None:
        runtime = await self._runtime(provider_session_id)
        await runtime.page.locator(selector).first.wait_for(
            timeout=int(max(1, timeout_seconds) * 1000)
        )

    async def screenshot(
        self,
        *,
        provider_session_id: str | None,
    ) -> ProviderScreenshotResult:
        runtime = await self._runtime(provider_session_id)
        image = await runtime.page.screenshot(type="png", full_page=True)
        return ProviderScreenshotResult(image_bytes=image)
