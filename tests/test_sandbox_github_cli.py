"""Tests for sandboxed CLI GitHub commands."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from ash_sandbox_cli.commands.github import app
from typer.testing import CliRunner


def _completed(
    args: list[str], stdout: str = "ok\n"
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args, returncode=0, stdout=stdout, stderr=""
    )


def test_repos_lists_owner_repositories() -> None:
    runner = CliRunner()

    with patch("ash_sandbox_cli.commands.github._run") as run:
        run.return_value = _completed(["gh"])

        result = runner.invoke(app, ["repos", "acme", "--limit", "25"])

    assert result.exit_code == 0
    command = run.call_args.args[0]
    assert command[:5] == ["gh", "repo", "list", "acme", "--limit"]
    assert "25" in command
    assert "--json" in command


def test_clone_uses_workspace_git_owner_repo_path() -> None:
    runner = CliRunner()

    with (
        patch("ash_sandbox_cli.commands.github._run") as run,
        patch.object(Path, "mkdir") as mkdir,
    ):
        run.return_value = _completed(["gh"])

        result = runner.invoke(app, ["clone", "acme/widget"])

    assert result.exit_code == 0
    mkdir.assert_called_once_with(parents=True, exist_ok=True)
    assert run.call_args.args[0] == [
        "gh",
        "repo",
        "clone",
        "acme/widget",
        "/workspace/git/acme/widget",
    ]


def test_create_defaults_to_private() -> None:
    runner = CliRunner()

    with patch("ash_sandbox_cli.commands.github._run") as run:
        run.return_value = _completed(["gh"])

        result = runner.invoke(app, ["create", "acme/widget"])

    assert result.exit_code == 0
    assert run.call_args.args[0] == ["gh", "repo", "create", "acme/widget", "--private"]


def test_create_allows_public() -> None:
    runner = CliRunner()

    with patch("ash_sandbox_cli.commands.github._run") as run:
        run.return_value = _completed(["gh"])

        result = runner.invoke(app, ["create", "acme/widget", "--public"])

    assert result.exit_code == 0
    assert run.call_args.args[0] == ["gh", "repo", "create", "acme/widget", "--public"]
