"""Quality worker commands. Spec: specs/quality.md."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

from ash.config.loader import load_config
from ash.quality.state import State
from ash.quality.worker import BraintrustStore, Worker


async def create_worker(state: State, repo: Path) -> Worker:
    import braintrust
    from openai import AsyncOpenAI

    config = load_config()
    if (
        not config.quality.enabled
        or not config.braintrust.enabled
        or not config.braintrust.api_key
    ):
        raise ValueError(
            "Enable [quality] and configure Braintrust before starting the worker"
        )
    key = config.resolve_provider_api_key("openai")
    if not key:
        raise ValueError("An OpenAI API key is required for scoring")
    logger = await asyncio.to_thread(
        braintrust.init_logger,
        project=config.braintrust.project,
        api_key=config.braintrust.api_key.get_secret_value(),
        set_current=False,
    )
    await asyncio.to_thread(lambda: logger.id)
    store = BraintrustStore(logger, config.braintrust.api_key.get_secret_value())
    sentry: Any = None
    if config.sentry and config.sentry.dsn:
        import sentry_sdk

        sentry = sentry_sdk.Client(
            dsn=config.sentry.dsn.get_secret_value(),
            default_integrations=False,
            release=config.sentry.release,
            environment=config.sentry.environment,
            send_default_pii=False,
        )
    return Worker(
        state,
        config.quality,
        store,
        AsyncOpenAI(api_key=key.get_secret_value(), timeout=60, max_retries=0),
        repo,
        sentry,
    )


async def run(args: argparse.Namespace, state: State) -> None:
    if args.command == "status":
        print(json.dumps(state.status(), indent=2))
        return
    if args.command == "retry":
        job = state.get(args.case_id)
        if job["repair"] not in (
            "validation_failed",
            "needs_reproduction",
            "interrupted",
        ):
            raise ValueError("Only failed repair candidates can be retried")
        directory = state.directory / "candidates" / args.case_id
        if directory.exists():
            import time

            directory.rename(
                directory.with_name(directory.name + f"-attempt-{time.time_ns()}")
            )
        state.update(args.case_id, repair="pending", published=0)
        return
    worker = await create_worker(state, args.repo.resolve())
    try:
        if args.command == "repair":
            await worker.repair(args.case_id)
        elif args.command == "once":
            await worker.once()
        else:
            while True:
                try:
                    await worker.once()
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "event": "quality_poll_failed",
                                "error_type": type(exc).__name__,
                            }
                        ),
                        flush=True,
                    )
                await asyncio.sleep(worker.config.poll_seconds)
    finally:
        await worker.client.close()
        if worker.sentry:
            await asyncio.to_thread(worker.sentry.close)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Score Ash evidence and build isolated repair candidates"
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".ash" / "quality"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("once", "serve", "status"):
        commands.add_parser(name)
    for name in ("repair", "retry"):
        commands.add_parser(name).add_argument("case_id")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(State.inspect(args.state_dir), indent=2))
        return
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(args.state_dir / "worker.lock"), timeout=0):
        state = State(args.state_dir)
        try:
            asyncio.run(run(args, state))
        finally:
            state.db.close()


if __name__ == "__main__":
    main()
