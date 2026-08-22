"""Repo commands for agent-facing coding workflows."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Inspect and operate on the current git repository.")


def _repo() -> Path:
    return Path.cwd()


def _run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - command lists are controlled by explicit repo subcommands.
        command,
        cwd=_repo(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _print_result(label: str, result: subprocess.CompletedProcess[str]) -> None:
    typer.echo(f"Repo action: {label}")
    typer.echo(f"Repo path: {_repo()}")
    typer.echo(f"Exit code: {result.returncode}")
    typer.echo("Output:")
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    typer.echo(output or "(no output)")


@app.command("status")
def status() -> None:
    """Show branch and short git status."""
    _print_result("status", _run(["git", "status", "--short", "--branch"]))


@app.command("changed-files")
def changed_files() -> None:
    """Show changed and untracked files."""
    diff = _run(["git", "diff", "--name-status", "HEAD"])
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard"])
    combined = subprocess.CompletedProcess(
        args=["ash-sb", "repo", "changed-files"],
        returncode=diff.returncode or untracked.returncode,
        stdout=(diff.stdout or "") + (untracked.stdout or ""),
        stderr=(diff.stderr or "") + (untracked.stderr or ""),
    )
    _print_result("changed_files", combined)


@app.command("diff")
def diff(base: Annotated[str, typer.Option(help="Base ref to diff against.")] = "HEAD") -> None:
    """Show diff stat and patch."""
    stat = _run(["git", "diff", "--stat", base])
    patch = _run(["git", "diff", base])
    combined = subprocess.CompletedProcess(
        args=["ash-sb", "repo", "diff"],
        returncode=stat.returncode or patch.returncode,
        stdout=(stat.stdout or "") + "\n--- DIFF ---\n" + (patch.stdout or ""),
        stderr=(stat.stderr or "") + (patch.stderr or ""),
    )
    _print_result("diff", combined)


@app.command("branch")
def branch(name: Annotated[str, typer.Argument(help="Branch name to create/switch to.")]) -> None:
    """Create or switch to a branch."""
    result = _run(["git", "switch", "-c", name])
    if result.returncode != 0:
        result = _run(["git", "switch", name])
    _print_result("branch", result)


@app.command("test")
def test(command: Annotated[list[str], typer.Argument(help="Command to run.")]) -> None:
    """Run a test command and echo self-verifying output."""
    if not command:
        typer.echo("Error: provide a test command, e.g. ash-sb repo test uv run pytest tests/test_cli.py")
        raise typer.Exit(2)
    result = _run(command, timeout=600)
    _print_result("test", result)
    raise typer.Exit(result.returncode)


@app.command("pr-summary")
def pr_summary(base: Annotated[str, typer.Option(help="Base ref to compare.")] = "HEAD") -> None:
    """Show branch, changed files, and diff stat for PR drafting."""
    branch_result = _run(["git", "branch", "--show-current"])
    files = _run(["git", "diff", "--name-status", base])
    stat = _run(["git", "diff", "--stat", base])
    typer.echo("Repo action: pr_summary")
    typer.echo(f"Repo path: {_repo()}")
    typer.echo(f"Exit code: {branch_result.returncode or files.returncode or stat.returncode}")
    typer.echo(f"Branch: {(branch_result.stdout or '').strip() or '(unknown)'}")
    typer.echo("Changed files:")
    typer.echo(files.stdout or "(none)")
    typer.echo("Diff stat:")
    typer.echo(stat.stdout or "(none)")
