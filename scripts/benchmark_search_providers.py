#!/usr/bin/env python3
"""Compare Exa and Parallel on the same small, auditable query set."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ash.config import load_config
from ash.tools.base import ToolContext
from ash.tools.builtin import ExaSearchTool, WebSearchTool


@dataclass(slots=True)
class Observation:
    provider: str
    query: str
    success: bool
    latency_ms: int
    result_count: int
    expected_domain_hit: bool
    error: str | None = None


def _domains_from_content(content: str) -> set[str]:
    domains: set[str] = set()
    for token in content.split():
        if token.startswith(("http://", "https://")):
            host = urlparse(token.rstrip(")],.")).netloc.removeprefix("www.")
            if host:
                domains.add(host)
    return domains


async def _run_one(provider: str, tool, case: dict[str, Any]) -> Observation:
    query = str(case["query"])
    started = time.perf_counter()
    result = await tool.execute({"query": query, "count": 5}, ToolContext())
    latency_ms = round((time.perf_counter() - started) * 1000)
    expected = {str(value) for value in case.get("expected_domains", [])}
    domains = set(result.metadata.get("domains", [])) or _domains_from_content(
        result.content
    )
    domain_hit = not expected or any(
        actual == wanted or actual.endswith(f".{wanted}")
        for actual in domains
        for wanted in expected
    )
    return Observation(
        provider=provider,
        query=query,
        success=not result.is_error,
        latency_ms=latency_ms,
        result_count=int(result.metadata.get("result_count", 0)),
        expected_domain_hit=domain_hit,
        error=result.content[:300] if result.is_error else None,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--queries",
        type=Path,
        default=Path("benchmarks/search_queries.json"),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    config = load_config()
    missing: list[str] = []
    exa_config = config.exa_search
    parallel_config = config.parallel_search
    if not exa_config or not exa_config.api_key:
        missing.append("EXA_API_KEY or [exa_search].api_key")
    if not parallel_config or not parallel_config.api_key:
        missing.append("PARALLEL_API_KEY or [parallel_search].api_key")
    if missing:
        parser.error("missing " + " and ".join(missing))
    assert exa_config is not None and exa_config.api_key is not None
    assert parallel_config is not None and parallel_config.api_key is not None

    cases = json.loads(args.queries.read_text())
    exa = ExaSearchTool(
        api_key=exa_config.api_key.get_secret_value(),
        sandbox_config=config.sandbox,
        workspace_path=config.workspace,
    )
    parallel = WebSearchTool(
        api_key=parallel_config.api_key.get_secret_value(),
        sandbox_config=config.sandbox,
        workspace_path=config.workspace,
    )
    observations: list[Observation] = []
    try:
        for case in cases:
            pair = await asyncio.gather(
                _run_one("exa", exa, case),
                _run_one("parallel", parallel, case),
            )
            observations.extend(pair)
    finally:
        await exa.cleanup()
        await parallel.cleanup()

    if args.as_json:
        print(json.dumps([asdict(item) for item in observations], indent=2))
        return 0

    print("provider  success  domain_hits  median_ms")
    for provider in ("exa", "parallel"):
        rows = [item for item in observations if item.provider == provider]
        successes = sum(item.success for item in rows)
        hits = sum(item.expected_domain_hit for item in rows)
        median = round(statistics.median(item.latency_ms for item in rows))
        print(
            f"{provider:<8}  {successes}/{len(rows):<5}  {hits}/{len(rows):<9}  {median}"
        )
    for item in observations:
        if item.error:
            print(f"ERROR {item.provider} {item.query}: {item.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
