"""Run the DeepAgents-backed research pipeline from the CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from ash.cli.console import console, error, warning
from ash.config import load_config
from ash.research import ResearchRequest, ResearchService

ResearchMode = Literal["smoke", "demo", "full"]


def register(app: typer.Typer) -> None:
    """Register the research command."""

    @app.command()
    def research(
        question: Annotated[
            str,
            typer.Argument(help="Research question or task"),
        ],
        config_path: Annotated[
            Path | None,
            typer.Option("--config", "-c", help="Path to configuration file"),
        ] = None,
        mode: Annotated[
            str,
            typer.Option("--mode", help="Research depth: smoke, demo, or full"),
        ] = "demo",
        deepagents_path: Annotated[
            Path | None,
            typer.Option(
                "--deepagents-path",
                help="Path to the local DeepAgents repo (defaults to auto-detect)",
            ),
        ] = None,
        deepagents_model: Annotated[
            str | None,
            typer.Option(
                "--deepagents-model",
                help="Model override passed to the DeepAgents runner",
            ),
        ] = None,
        max_search_results: Annotated[
            int | None,
            typer.Option(
                "--max-search-results",
                help="Override the DeepAgents search fanout limit",
            ),
        ] = None,
        codex_review: Annotated[
            bool,
            typer.Option(
                "--codex-review/--no-codex-review",
                help="Enable or disable the final Codex briefing pass",
            ),
        ] = True,
        codex_model_alias: Annotated[
            str | None,
            typer.Option(
                "--codex-model",
                help="Ash model alias for the final review pass (default: codex)",
            ),
        ] = "codex",
        email_results: Annotated[
            bool,
            typer.Option(
                "--email/--no-email",
                help=(
                    "Send research results by local email when a recipient is configured "
                    "(ASH_RESEARCH_EMAIL_TO or --email-to)"
                ),
            ),
        ] = False,
        email_to: Annotated[
            str | None,
            typer.Option(
                "--email-to",
                help="Override recipient email address for this run",
            ),
        ] = None,
    ) -> None:
        """Run research and persist artifacts under `~/.ash/research/jobs/`."""
        config = None
        try:
            config = load_config(config_path)
            from ash.logging import configure_logging

            configure_logging(level="DEBUG")
            if config.sentry:
                from ash.observability import init_sentry

                init_sentry(config.sentry)
        except FileNotFoundError:
            if codex_review:
                warning(
                    "No Ash config found. Continuing without Codex review. "
                    "Run 'ash config init' if you want model-backed post-processing."
                )
                codex_review = False

        request = ResearchRequest(
            question=question,
            mode=cast(ResearchMode, mode)
            if mode in {"smoke", "demo", "full"}
            else "demo",
            deepagents_path=deepagents_path,
            deepagents_model=deepagents_model,
            max_search_results=max_search_results,
            codex_review=codex_review,
            codex_model_alias=codex_model_alias,
            email_results=email_results,
            email_to=email_to,
        )

        try:
            result = asyncio.run(ResearchService(config=config).run(request))
        except ValueError as exc:
            error(str(exc))
            raise typer.Exit(1) from None

        console.print(result.to_user_message())
        if result.status == "failed":
            raise typer.Exit(1)
