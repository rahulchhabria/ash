"""Web search tool using Parallel Search API, executed in sandbox."""

import json
import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ash.llm.retry import RetryConfig, with_retry
from ash.sandbox import SandboxExecutor
from ash.tools.base import Tool, ToolContext, ToolResult, build_sandbox_manager_config
from ash.tools.builtin.search_cache import SearchCache
from ash.tools.builtin.search_types import SearchResponse

if TYPE_CHECKING:
    from ash.config.models import SandboxConfig

logger = logging.getLogger(__name__)

PARALLEL_SEARCH_URL = "https://api.parallel.ai/v1/search"


def _extract_domains(response: SearchResponse) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for result in response.results:
        host = (urlparse(result.url).netloc or "").strip().lower()
        if not host:
            continue
        if host.startswith("www."):
            host = host[4:]
        if host in seen:
            continue
        seen.add(host)
        domains.append(host)
    return domains


# Python script to execute inside sandbox
# Outputs JSON for reliable parsing and accurate result counting
# Supports: query, count, freshness, country, search_type
SEARCH_SCRIPT = '''
import json, os, sys, urllib.request, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# Parse arguments: query count freshness country search_type
query = sys.argv[1]
count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
freshness = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else None
country = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "none" else None
search_type = sys.argv[5] if len(sys.argv) > 5 else "web"

api_key = os.environ.get("PARALLEL_API_KEY", "")
if not api_key:
    print(json.dumps({"error": "PARALLEL_API_KEY not set", "code": 500}))
    sys.exit(1)

def after_date_for_freshness(value):
    days = {
        "pd": 1,
        "pw": 7,
        "pm": 31,
        "py": 366,
    }.get(value)
    if not days:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.date().isoformat()

def search_query_for_objective(value):
    query = " ".join(value.split())
    if len(query) <= 200:
        return query
    return query[:200].rsplit(" ", 1)[0] or query[:200]

objective = query
if search_type == "news":
    objective = f"{query}. Focus on recent news coverage and announcements."

advanced_settings = {
    "max_results": count,
}
source_policy = {}
if freshness_date := after_date_for_freshness(freshness):
    source_policy["after_date"] = freshness_date
if source_policy:
    advanced_settings["source_policy"] = source_policy
if country:
    advanced_settings["location"] = country.lower()

payload = {
    "objective": objective,
    "search_queries": [search_query_for_objective(query)],
    "mode": "advanced",
    "advanced_settings": advanced_settings,
}

start_time = time.time()

try:
    req = urllib.request.Request(
        "https://api.parallel.ai/v1/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            print(json.dumps({"error": f"HTTP {resp.status}", "code": resp.status}))
            sys.exit(1)
        data = json.load(resp)
except urllib.error.HTTPError as e:
    error_msg = {
        401: "Invalid API key",
        429: "Rate limit exceeded",
    }.get(e.code, f"HTTP {e.code}")
    print(json.dumps({"error": error_msg, "code": e.code}))
    sys.exit(1)
except urllib.error.URLError as e:
    print(json.dumps({"error": str(e.reason), "code": 0}))
    sys.exit(1)
except Exception as e:
    print(json.dumps({"error": str(e), "code": 0}))
    sys.exit(1)

search_time_ms = int((time.time() - start_time) * 1000)

def truncate_at_word(text, max_len=300):
    """Truncate at word boundary, not mid-word."""
    if len(text) <= max_len:
        return text
    # Find last space before max_len
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.7:  # Only use if space is reasonably close
        truncated = truncated[:last_space]
    return truncated.rstrip() + "..."

def extract_site_name(url_str):
    """Extract readable site name from URL."""
    try:
        parsed = urlparse(url_str)
        domain = parsed.netloc
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return None

raw_results = data.get("results", [])

results = []

for r in raw_results:
    title = r.get("title", "No title")
    result_url = r.get("url", "")
    excerpts = r.get("excerpts") or []
    desc = "\\n\\n".join(excerpts) if excerpts else r.get("description", "")

    # Truncate at word boundary
    if desc:
        desc = truncate_at_word(desc, 300)

    results.append({
        "title": title,
        "url": result_url,
        "description": desc,
        "site_name": extract_site_name(result_url),
        "published_date": r.get("publish_date"),
    })

output = {
    "query": query,
    "results": results,
    "total_count": len(results),
    "search_time_ms": search_time_ms,
    "search_type": search_type,
}

print(json.dumps(output))
'''


class WebSearchTool(Tool):
    """Search the web using Parallel Search API.

    All requests execute inside the Docker sandbox for network control.
    Requires network_mode: bridge in sandbox configuration.

    Features:
    - Structured JSON output with citation metadata
    - In-memory caching with 15-min TTL
    - Retry support for transient errors (via retry.py)
    """

    def __init__(
        self,
        api_key: str,
        executor: SandboxExecutor | None = None,
        sandbox_config: "SandboxConfig | None" = None,
        workspace_path: Path | None = None,
        cache: SearchCache | None = None,
        retry_config: RetryConfig | None = None,
        max_results: int = 20,
    ):
        """Initialize web search tool.

        Args:
            api_key: Parallel Search API key.
            executor: Shared sandbox executor (preferred).
            sandbox_config: Sandbox configuration (used if executor not provided).
            workspace_path: Path to workspace (for sandbox config).
            cache: Optional search cache for result caching.
            retry_config: Optional retry configuration for transient errors.
            max_results: Maximum results to return per search (max 20).
        """
        self._api_key = api_key
        self._max_results = max_results
        self._cache = cache
        self._retry_config = retry_config or RetryConfig()

        if executor:
            self._executor = executor
        else:
            # Check network mode
            network_mode = sandbox_config.network_mode if sandbox_config else "bridge"
            if network_mode == "none":
                raise ValueError(
                    "Web search requires network_mode: bridge in sandbox configuration"
                )

            # Build sandbox config with API key in environment
            manager_config = build_sandbox_manager_config(
                sandbox_config, workspace_path, default_network_mode="bridge"
            )
            self._executor = SandboxExecutor(
                config=manager_config,
                environment={"PARALLEL_API_KEY": api_key},
            )

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for current information using the Parallel Search API. "
            "Use this to find recent news, documentation, articles, or any "
            "information that may not be in your training data. Prefer this "
            "for discovery before `web_fetch`/`browser`. "
            "Returns structured results with titles, URLs, and descriptions."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "count": {
                    "type": "integer",
                    "description": f"Number of results (max {self._max_results}).",
                    "default": 5,
                },
                "freshness": {
                    "type": "string",
                    "enum": ["pd", "pw", "pm", "py"],
                    "description": (
                        "Filter by content freshness: "
                        "'pd' (past day), 'pw' (past week), "
                        "'pm' (past month), 'py' (past year)."
                    ),
                },
                "country": {
                    "type": "string",
                    "description": (
                        "Two-letter country code for localized results (e.g., 'US', 'GB', 'DE')."
                    ),
                },
                "search_type": {
                    "type": "string",
                    "enum": ["web", "news"],
                    "description": "Type of search: 'web' (default) or 'news' for news articles.",
                    "default": "web",
                },
            },
            "required": ["query"],
        }

    # Valid parameter values
    VALID_FRESHNESS = {"pd", "pw", "pm", "py"}
    VALID_SEARCH_TYPES = {"web", "news"}

    async def execute(
        self,
        input_data: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        query = input_data.get("query", "").strip()
        if not query:
            return ToolResult.error("Missing required parameter: query")

        count = min(input_data.get("count", 5), self._max_results)
        freshness = input_data.get("freshness")
        country = input_data.get("country")
        search_type = input_data.get("search_type", "web")

        # Validate optional parameters
        if freshness and freshness not in self.VALID_FRESHNESS:
            return ToolResult.error(
                f"Invalid freshness: {freshness}. Must be one of: pd, pw, pm, py"
            )
        if search_type not in self.VALID_SEARCH_TYPES:
            return ToolResult.error(
                f"Invalid search_type: {search_type}. Must be 'web' or 'news'"
            )
        if country and (len(country) != 2 or not country.isalpha()):
            return ToolResult.error(
                f"Invalid country: {country}. Must be a 2-letter code (e.g., 'US', 'GB')"
            )

        # Build cache key including parameters
        cache_key = f"{query}|{count}|{freshness}|{country}|{search_type}"

        if self._cache:
            cached = self._cache.get(cache_key)
            if cached is not None and isinstance(cached, SearchResponse):
                logger.debug(f"Cache hit for query: {query}")
                return ToolResult.success(
                    cached.to_formatted_text(),
                    result_count=len(cached.results),
                    cached=True,
                    search_time_ms=cached.search_time_ms,
                    search_type=search_type,
                    domains=_extract_domains(cached),
                )

        async def do_search() -> SearchResponse:
            escaped_query = shlex.quote(query)
            # Pass all parameters: query count freshness country search_type
            freshness_arg = shlex.quote(freshness) if freshness else "none"
            country_arg = shlex.quote(country) if country else "none"
            search_type_arg = shlex.quote(search_type)

            command = (
                f"python3 -c {shlex.quote(SEARCH_SCRIPT)} "
                f"{escaped_query} {count} {freshness_arg} {country_arg} {search_type_arg}"
            )

            result = await self._executor.execute(
                command,
                timeout=30,
                reuse_container=True,
                environment={"PARALLEL_API_KEY": self._api_key},
            )

            if result.timed_out:
                raise TimeoutError("Search request timed out")

            output = result.stdout.strip() if result.stdout else ""
            if not output:
                raise ValueError("Empty response from search")

            data = json.loads(output)
            if "error" in data:
                raise Exception(f"{data['error']} (code: {data.get('code', 0)})")

            return SearchResponse.from_json(output)

        try:
            response = await with_retry(
                do_search,
                config=self._retry_config,
                operation_name="Parallel Search",
            )

            if self._cache and not response.cached:
                self._cache.set(cache_key, response)

            return ToolResult.success(
                response.to_formatted_text(),
                result_count=len(response.results),
                cached=response.cached,
                search_time_ms=response.search_time_ms,
                search_type=search_type,
                domains=_extract_domains(response),
            )

        except Exception as e:
            logger.exception(f"Search error for query: {query}")
            return ToolResult.error(f"Search error: {e}")

    async def cleanup(self) -> None:
        """Clean up sandbox resources."""
        if self._executor:
            await self._executor.cleanup()
