"""Tests for portable host discovery and Omarchy migration checks."""

from pathlib import Path

from ash.cli.commands.doctor import (
    _check_migration_state,
    _check_omarchy_target,
    _check_runtime_artifacts,
    run_doctor_checks,
)
from ash.config.paths import ENV_VAR, get_ash_home
from ash.host import HostEnvironment, _read_os_release


def _host(*, commands: frozenset[str]) -> HostEnvironment:
    return HostEnvironment(
        platform="linux",
        distro_id="omarchy",
        distro_like=("arch",),
        session_type="wayland",
        desktop="Hyprland",
        wayland_display="wayland-1",
        commands=commands,
    )


def test_read_os_release_parses_quotes_and_comments(tmp_path: Path) -> None:
    release = tmp_path / "os-release"
    release.write_text('ID="omarchy"\nID_LIKE="arch linux"\n# ignored\n')

    assert _read_os_release(release) == {
        "ID": "omarchy",
        "ID_LIKE": "arch linux",
    }


def test_omarchy_target_reports_ready_host(monkeypatch, tmp_path: Path) -> None:
    ash_home = tmp_path / ".ash"
    ash_home.mkdir(mode=0o700, exist_ok=True)
    ash_home.chmod(0o700)
    (ash_home / "config.toml").write_text("")
    (ash_home / "config.toml").chmod(0o600)
    (ash_home / "vault").mkdir(mode=0o700)
    monkeypatch.setenv(ENV_VAR, str(ash_home))
    get_ash_home.cache_clear()
    try:
        commands = frozenset(
            {
                "python3",
                "uv",
                "git",
                "docker",
                "systemctl",
                "hyprctl",
                "wl-copy",
                "notify-send",
                "grim",
                "slurp",
                "omarchy",
            }
        )
        findings = _check_omarchy_target(_host(commands=commands))
    finally:
        get_ash_home.cache_clear()

    assert all(finding.level == "ok" for finding in findings)
    assert any(finding.check == "omarchy.distribution" for finding in findings)


def test_omarchy_target_warns_for_missing_runtime_and_broken_link(
    monkeypatch, tmp_path: Path
) -> None:
    ash_home = tmp_path / ".ash"
    local_skills = ash_home / "skills.installed" / "local"
    local_skills.mkdir(parents=True)
    (local_skills / "old-host-skill").symlink_to(tmp_path / "missing")
    monkeypatch.setenv(ENV_VAR, str(ash_home))
    get_ash_home.cache_clear()
    try:
        findings = _check_omarchy_target(_host(commands=frozenset()))
    finally:
        get_ash_home.cache_clear()

    assert any(
        finding.check == "omarchy.command.docker" and finding.level == "error"
        for finding in findings
    )
    assert any(
        finding.check == "omarchy.migration.local_skill_links"
        and "old-host-skill" in finding.detail
        for finding in findings
    )


def test_general_doctor_omits_target_checks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_VAR, str(tmp_path / ".ash"))
    get_ash_home.cache_clear()
    try:
        result = run_doctor_checks()
    finally:
        get_ash_home.cache_clear()

    assert not any(finding.check.startswith("omarchy.") for finding in result.findings)


def test_empty_pid_file_is_reported_without_crashing(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "ash.pid").write_text("")
    monkeypatch.setattr("ash.cli.commands.doctor.get_run_path", lambda: tmp_path)
    findings = _check_runtime_artifacts()
    assert any(
        finding.check == "run.pid" and finding.level == "warning"
        for finding in findings
    )


def test_broken_local_skills_root_link_is_reported(monkeypatch, tmp_path: Path) -> None:
    ash_home = tmp_path / ".ash"
    root = ash_home / "skills.installed"
    root.mkdir(parents=True)
    (root / "local").symlink_to(tmp_path / "missing")
    monkeypatch.setenv(ENV_VAR, str(ash_home))
    get_ash_home.cache_clear()
    try:
        findings = _check_migration_state()
    finally:
        get_ash_home.cache_clear()
    assert any("root link" in finding.detail for finding in findings)
