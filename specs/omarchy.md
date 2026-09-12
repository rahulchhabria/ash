# Omarchy Migration Readiness

> Read-only host diagnostics and migration guidance for running Ash on Omarchy

Files: src/ash/host.py, src/ash/cli/commands/doctor.py, docs/src/content/docs/getting-started/omarchy.mdx

## Requirements

### MUST

- Expose `ash doctor --target omarchy` as a read-only readiness check
- Detect Linux distribution metadata without assuming `/etc/os-release` exists
- Check Ash runtime prerequisites: Python, uv, Git, Docker, and systemd
- Verify Python 3.12+, current-user Docker daemon access, and the systemd user manager
- Report Wayland, Hyprland, and optional desktop-helper availability
- Check migration-sensitive permissions without printing secret values
- Identify broken local installed-skill symlinks after a move
- Keep desktop control out of the migration checker

### SHOULD

- Treat desktop helpers as non-blocking migration warnings
- Provide repairs using Omarchy/Arch package terminology
- Document copying `$ASH_HOME` separately from the source checkout
- Require reinstallation of the systemd user service on the destination

### MAY

- Add scoped desktop integrations after the migration

## Interface

```bash
ash doctor --target omarchy
```

```python
detect_host_environment() -> HostEnvironment
run_doctor_checks(target: str | None = None) -> DoctorResult
```

## Behaviors

| Input | Output | Notes |
|-------|--------|-------|
| `ash doctor` | Existing general checks | No target checks |
| `ash doctor --target omarchy` | General + Omarchy readiness findings | Read-only |
| Missing desktop helper | Warning | Does not block headless Ash |
| Broken local skill link | Warning with link name | Does not print credentials |
| Unknown target | CLI usage error | Supported target is `omarchy` |

## Errors

| Condition | Response |
|-----------|----------|
| `/etc/os-release` missing or malformed | Distribution reported as unknown |
| Required command missing or unusable | Error with install/repair guidance |
| `$ASH_HOME` missing | Existing home warning remains authoritative |

## Verification

```bash
uv run pytest tests/test_omarchy_readiness.py tests/test_cli.py -q
uv run ash doctor --target omarchy
```

- The command performs no writes
- Findings never contain config or vault secret values
- Existing `ash doctor` behavior remains compatible
