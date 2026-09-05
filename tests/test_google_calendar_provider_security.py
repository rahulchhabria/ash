from __future__ import annotations

import json
import runpy
import stat
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "google_calendar_provider.py"


def test_calendar_token_cache_is_private(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(SCRIPT_PATH))
    writer = namespace["_write_token_cache"]
    token_path = tmp_path / "calendar" / "token-cache.json"
    writer.__globals__["TOKEN_CACHE_PATH"] = token_path
    payload = {"access_token": "test-token"}  # noqa: S105
    writer(payload)

    assert json.loads(token_path.read_text()) == payload
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
