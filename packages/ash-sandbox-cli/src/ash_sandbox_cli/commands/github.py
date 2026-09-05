"""GitHub commands for agent-facing repository workflows."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Inspect and operate on GitHub repositories.")

GIT_ROOT = Path("/workspace/git")


def _run(
    command: list[str], *, cwd: Path | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - command lists are controlled by explicit github subcommands.
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _print_result(label: str, result: subprocess.CompletedProcess[str]) -> None:
    typer.echo(f"GitHub action: {label}")
    typer.echo(f"Exit code: {result.returncode}")
    typer.echo("Output:")
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    typer.echo(output or "(no output)")
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


def _repo_dest(repo: str, dest: str | None = None) -> Path:
    if dest:
        path = Path(dest)
        return path if path.is_absolute() else GIT_ROOT / path

    if "/" not in repo:
        raise typer.BadParameter("repo must be in owner/name format")

    owner, name = repo.split("/", 1)
    return GIT_ROOT / owner / name


@app.command("auth-status")
def auth_status() -> None:
    """Show GitHub CLI authentication status."""
    _print_result("auth_status", _run(["gh", "auth", "status"]))


@app.command("orgs")
def orgs(
    account: Annotated[
        str | None,
        typer.Option(
            help="GitHub user to list orgs for. Defaults to authenticated user."
        ),
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum orgs to return.")] = 100,
) -> None:
    """List organizations visible to the authenticated GitHub account."""
    command = ["gh", "org", "list", "--limit", str(limit)]
    if account:
        command.append(account)
    _print_result("orgs", _run(command))


@app.command("repos")
def repos(
    owner: Annotated[str, typer.Argument(help="GitHub user or org name.")],
    limit: Annotated[int, typer.Option(help="Maximum repos to return.")] = 100,
    visibility: Annotated[
        str | None,
        typer.Option(help="Optional visibility filter: public, private, internal."),
    ] = None,
) -> None:
    """List repositories for a GitHub user or organization."""
    fields = "name,nameWithOwner,description,isPrivate,visibility,url,defaultBranchRef"
    command = ["gh", "repo", "list", owner, "--limit", str(limit), "--json", fields]
    if visibility:
        command.extend(["--visibility", visibility])
    _print_result("repos", _run(command))


@app.command("clone")
def clone(
    repo: Annotated[str, typer.Argument(help="Repository in owner/name format.")],
    dest: Annotated[
        str | None,
        typer.Option(help="Destination path. Relative paths are under /workspace/git."),
    ] = None,
) -> None:
    """Clone a GitHub repository into /workspace/git."""
    destination = _repo_dest(repo, dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["gh", "repo", "clone", repo, str(destination)]
    _print_result("clone", _run(command, timeout=600))
    typer.echo(f"Local path: {destination}")


@app.command("create")
def create(
    repo: Annotated[str, typer.Argument(help="Repository in owner/name format.")],
    private: Annotated[
        bool, typer.Option("--private", help="Create a private repository.")
    ] = False,
    public: Annotated[
        bool, typer.Option("--public", help="Create a public repository.")
    ] = False,
    source: Annotated[
        str | None,
        typer.Option(
            help="Optional local source path to push as the initial repository contents."
        ),
    ] = None,
) -> None:
    """Create a GitHub repository, optionally from a local source checkout."""
    if private and public:
        typer.echo("Error: choose only one of --private or --public")
        raise typer.Exit(2)

    visibility = "--public" if public else "--private"
    command = ["gh", "repo", "create", repo, visibility]
    cwd = None
    if source:
        cwd = Path(source)
        command.extend(["--source", str(cwd), "--remote", "origin", "--push"])
    _print_result("create", _run(command, cwd=cwd, timeout=600))


@app.command("view")
def view(
    repo: Annotated[str, typer.Argument(help="Repository in owner/name format.")],
) -> None:
    """Show repository metadata."""
    command = [
        "gh",
        "repo",
        "view",
        repo,
        "--json",
        "nameWithOwner,description,isPrivate,visibility,url,defaultBranchRef,viewerPermission",
    ]
    _print_result("view", _run(command))


@app.command("run")
def run(
    args: Annotated[list[str], typer.Argument(help="Arguments passed to gh.")],
) -> None:
    """Run a gh command when a specific wrapper is not available."""
    if not args:
        typer.echo(
            "Error: provide gh arguments, e.g. ash-sb github run repo list OWNER"
        )
        raise typer.Exit(2)
    typer.echo(f"GitHub command: gh {shlex.join(args)}")
    _print_result("run", _run(["gh", *args], timeout=600))
