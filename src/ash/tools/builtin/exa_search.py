"""Exa web search tool executed in a dedicated network sandbox."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ash.sandbox import SandboxExecutor
from ash.tools.base import Tool, ToolContext, ToolResult, build_sandbox_manager_config
from ash.tools.builtin.search_cache import SearchCache, SearchCacheRecord

if TYPE_CHECKING:
    from ash.config.models import SandboxConfig


EXA_SEARCH_SCRIPT = r"""
import json, os, sys, urllib.error, urllib.request

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

query = sys.argv[1]
count = int(sys.argv[2])
search_type = sys.argv[3]
api_key = os.environ.get("EXA_API_KEY", "")
if not api_key:
    print(json.dumps({"error": "EXA_API_KEY not set", "code": 500}))
    raise SystemExit(1)

payload = {
    "query": query,
    "type": search_type,
    "numResults": count,
    "contents": {"highlights": {"maxCharacters": 1200}},
}
request = urllib.request.Request(
    "https://api.exa.ai/search",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
    },
    method="POST",
)
try:
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=30) as response:
        print(response.read().decode("utf-8"))
except urllib.error.HTTPError as error:
    detail = error.read().decode("utf-8", errors="replace")[:1000]
    print(json.dumps({"error": detail or str(error), "code": error.code}))
except Exception as error:
    print(json.dumps({"error": str(error), "code": 0}))
"""


class ExaSearchTool(Tool):
    """Search the public web through Exa."""

    def __init__(
        self,
        *,
        api_key: str,
        sandbox_config: SandboxConfig,
        workspace_path: Path,
        cache: SearchCache | None = None,
        max_results: int = 10,
    ) -> None:
        self._api_key = api_key
        self._cache = cache
        self._max_results = max_results
        manager_config = build_sandbox_manager_config(
            sandbox_config,
            workspace_path,
            default_network_mode="bridge",
            network_mode_override="bridge",
        )
        self._executor = SandboxExecutor(config=manager_config)

    @property
    def name(self) -> str:
        return "exa_search"

    @property
    def description(self) -> str:
        return (
            "Search the public web with Exa and return cited page highlights. "
            "Use as a fallback or for provider comparison, not for authoritative "
            "local-business hours when `google_places` is available."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "count": {
                    "type": "integer",
                    "default": 5,
                    "description": f"Number of results, up to {self._max_results}.",
                },
                "search_type": {
                    "type": "string",
                    "enum": ["instant", "fast", "auto"],
                    "default": "auto",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        del context
        query = str(input_data.get("query", "")).strip()
        if not query:
            return ToolResult.error("Missing required parameter: query")
        count = min(max(int(input_data.get("count", 5)), 1), self._max_results)
        search_type = str(input_data.get("search_type", "auto"))
        if search_type not in {"instant", "fast", "auto"}:
            return ToolResult.error("search_type must be instant, fast, or auto")

        cache_key = f"{query.lower()}|{count}|{search_type}"
        if self._cache and (cached := self._cache.get(cache_key)) is not None:
            if isinstance(cached, SearchCacheRecord):
                content = cached.content
                domains = list(cached.domains)
                result_count = cached.result_count
            else:
                content = str(cached)
                domains = _domains_from_formatted_result(content)
                result_count = sum(
                    1 for line in content.splitlines() if line[:1].isdigit()
                )
            return ToolResult.success(
                content,
                cached=True,
                provider="exa",
                domains=domains,
                result_count=result_count,
            )

        command = (
            f"python3 -c {shlex.quote(EXA_SEARCH_SCRIPT)} "
            f"{shlex.quote(query)} {count} {shlex.quote(search_type)}"
        )
        result = await self._executor.execute(
            command,
            timeout=35,
            reuse_container=False,
            environment={"EXA_API_KEY": self._api_key},
        )
        if result.timed_out:
            return ToolResult.error("Exa search timed out")
        output = result.stdout.strip() if result.stdout else ""
        if not output:
            return ToolResult.error("Exa search returned an empty response")
        try:
            data = json.loads(output)
        except json.JSONDecodeError as error:
            return ToolResult.error(f"Invalid Exa response: {error}")
        if "error" in data:
            return ToolResult.error(
                f"Exa search error: {data['error']} (code: {data.get('code', 0)})"
            )

        lines = [f"Exa results for: {query}"]
        domains: list[str] = []
        for index, item in enumerate(data.get("results", []), 1):
            title = str(item.get("title") or "Untitled")
            url = str(item.get("url") or "")
            highlights = item.get("highlights") or []
            excerpt = " ".join(str(value) for value in highlights).strip()
            lines.append(f"{index}. {title}\n   {url}\n   {excerpt}")
            host = urlparse(url).netloc.removeprefix("www.")
            if host and host not in domains:
                domains.append(host)
        formatted = "\n".join(lines)
        if self._cache:
            self._cache.set(
                cache_key,
                SearchCacheRecord(
                    content=formatted,
                    domains=tuple(domains),
                    result_count=len(data.get("results", [])),
                ),
            )
        return ToolResult.success(
            formatted,
            result_count=len(data.get("results", [])),
            domains=domains,
            provider="exa",
        )

    async def cleanup(self) -> None:
        await self._executor.cleanup()


def _domains_from_formatted_result(content: str) -> list[str]:
    domains: list[str] = []
    for token in content.split():
        if not token.startswith(("http://", "https://")):
            continue
        host = urlparse(token.rstrip(".,)")).netloc.removeprefix("www.")
        if host and host not in domains:
            domains.append(host)
    return domains
