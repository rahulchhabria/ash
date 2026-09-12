"""Google Places local-business lookup in a dedicated network sandbox."""

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


GOOGLE_PLACES_SCRIPT = r"""
import json, os, sys, urllib.error, urllib.request

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

query = sys.argv[1]
count = int(sys.argv[2])
latitude = None if sys.argv[3] == "none" else float(sys.argv[3])
longitude = None if sys.argv[4] == "none" else float(sys.argv[4])
radius = float(sys.argv[5])
api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
if not api_key:
    print(json.dumps({"error": "GOOGLE_MAPS_API_KEY not set", "code": 500}))
    raise SystemExit(1)

payload = {"textQuery": query, "maxResultCount": count}
if latitude is not None and longitude is not None:
    payload["locationBias"] = {
        "circle": {
            "center": {"latitude": latitude, "longitude": longitude},
            "radius": radius,
        }
    }

field_mask = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.businessStatus",
    "places.currentOpeningHours",
    "places.regularOpeningHours",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.googleMapsUri",
])
request = urllib.request.Request(
    "https://places.googleapis.com/v1/places:searchText",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    },
    method="POST",
)
try:
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=30) as response:
        print(response.read().decode("utf-8"))
except urllib.error.HTTPError as error:
    detail = error.read().decode("utf-8", errors="replace")[:1500]
    print(json.dumps({"error": detail or str(error), "code": error.code}))
except Exception as error:
    print(json.dumps({"error": str(error), "code": 0}))
"""


class GooglePlacesTool(Tool):
    """Resolve businesses and current opening hours using Google Places."""

    def __init__(
        self,
        *,
        api_key: str,
        sandbox_config: SandboxConfig,
        workspace_path: Path,
        cache: SearchCache | None = None,
        max_results: int = 5,
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
        return "google_places"

    @property
    def description(self) -> str:
        return (
            "Find a specific local business or place, including its address, current "
            "and regular opening hours, phone number, website, and Google Maps URL. "
            "Prefer this over general web search for store hours and branch resolution."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Business/place plus city, neighborhood, address, or cross streets."
                    ),
                },
                "count": {
                    "type": "integer",
                    "default": 3,
                    "description": f"Number of candidate places, up to {self._max_results}.",
                },
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
                "radius_meters": {
                    "type": "number",
                    "default": 25000,
                    "description": "Optional bias radius when coordinates are provided.",
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
        count = min(max(int(input_data.get("count", 3)), 1), self._max_results)
        latitude = input_data.get("latitude")
        longitude = input_data.get("longitude")
        if (latitude is None) != (longitude is None):
            return ToolResult.error("latitude and longitude must be supplied together")
        if latitude is not None and not -90 <= float(latitude) <= 90:
            return ToolResult.error("latitude must be between -90 and 90")
        if longitude is not None and not -180 <= float(longitude) <= 180:
            return ToolResult.error("longitude must be between -180 and 180")
        radius = min(max(float(input_data.get("radius_meters", 25000)), 1), 50000)
        latitude_arg = "none" if latitude is None else str(float(latitude))
        longitude_arg = "none" if longitude is None else str(float(longitude))
        cache_key = f"{query}|{count}|{latitude_arg}|{longitude_arg}|{radius}"
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
                provider="google_places",
                domains=domains,
                result_count=result_count,
            )

        command = (
            f"python3 -c {shlex.quote(GOOGLE_PLACES_SCRIPT)} "
            f"{shlex.quote(query)} {count} {latitude_arg} {longitude_arg} {radius}"
        )
        result = await self._executor.execute(
            command,
            timeout=35,
            reuse_container=False,
            environment={"GOOGLE_MAPS_API_KEY": self._api_key},
        )
        if result.timed_out:
            return ToolResult.error("Google Places lookup timed out")
        output = result.stdout.strip() if result.stdout else ""
        if not output:
            return ToolResult.error("Google Places returned an empty response")
        try:
            data = json.loads(output)
        except json.JSONDecodeError as error:
            return ToolResult.error(f"Invalid Google Places response: {error}")
        if "error" in data:
            return ToolResult.error(
                f"Google Places error: {data['error']} (code: {data.get('code', 0)})"
            )

        places = data.get("places", [])
        lines = [f"Google Places results for: {query}"]
        domains: list[str] = []
        for index, place in enumerate(places, 1):
            name = (place.get("displayName") or {}).get("text") or "Unknown place"
            lines.extend(
                [
                    f"{index}. {name}",
                    f"   Address: {place.get('formattedAddress') or 'not listed'}",
                    f"   Business status: {place.get('businessStatus') or 'unknown'}",
                    f"   Phone: {place.get('internationalPhoneNumber') or place.get('nationalPhoneNumber') or 'not listed'}",
                ]
            )
            hours = place.get("currentOpeningHours") or place.get("regularOpeningHours")
            if hours:
                lines.append(f"   Open now: {hours.get('openNow', 'unknown')}")
                descriptions = hours.get("weekdayDescriptions") or []
                if descriptions:
                    lines.append("   Hours: " + " | ".join(descriptions))
                if hours.get("nextOpenTime"):
                    lines.append(f"   Next open: {hours['nextOpenTime']}")
            lines.append(f"   Website: {place.get('websiteUri') or 'not listed'}")
            lines.append(f"   Maps: {place.get('googleMapsUri') or 'not listed'}")
            for url in (place.get("websiteUri"), place.get("googleMapsUri")):
                host = urlparse(str(url or "")).netloc.removeprefix("www.")
                if host and host not in domains:
                    domains.append(host)

        formatted = "\n".join(lines)
        if self._cache:
            self._cache.set(
                cache_key,
                SearchCacheRecord(
                    content=formatted,
                    domains=tuple(domains),
                    result_count=len(places),
                ),
            )
        return ToolResult.success(
            formatted,
            result_count=len(places),
            provider="google_places",
            domains=domains,
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
