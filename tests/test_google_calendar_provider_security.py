from __future__ import annotations

import json
import runpy
import stat
from pathlib import Path

import pytest

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


def test_historical_search_preserves_query_range_and_pagination(monkeypatch):
    namespace = runpy.run_path(str(SCRIPT_PATH))
    fn = namespace["_list_events"]
    calls = []
    monkeypatch.setitem(
        fn.__globals__,
        "_google_request",
        lambda *args, **kwargs: (
            calls.append(kwargs["params"]) or {"items": [], "nextPageToken": "next"}
        ),
    )
    output = fn(
        {
            "query": "optometrist",
            "time_min": "2025-01-01T00:00:00Z",
            "time_max": "2026-10-10T00:00:00Z",
            "page_token": "previous",
        },
        access_token="test",
    )
    params = dict(calls[0])
    assert params["q"] == "optometrist"
    assert params["timeMin"] == "2025-01-01T00:00:00Z"
    assert params["timeMax"] == "2026-10-10T00:00:00Z"
    assert params["pageToken"] == "previous"
    assert output["next_page_token"] == "next"


@pytest.mark.parametrize(
    "lower,upper",
    [("2026-10-10", "2026-10-11"), ("2026-10-11T00:00:00Z", "2026-10-10T00:00:00Z")],
)
def test_invalid_date_ranges_fail_before_request(monkeypatch, lower, upper):
    namespace = runpy.run_path(str(SCRIPT_PATH))
    fn = namespace["_list_events"]
    with pytest.raises(namespace["BridgeError"]):
        fn({"time_min": lower, "time_max": upper}, access_token="test")


def test_google_provider_advertises_both_scoped_capabilities():
    namespace = runpy.run_path(str(SCRIPT_PATH))
    result = namespace["_dispatch"](
        {"id": "probe", "version": 1, "method": "definitions", "params": {}}
    )
    definitions = {item["id"]: item for item in result["result"]["definitions"]}
    assert set(definitions) == {"gog.calendar", "gog.email"}
    assert all(op["requires_auth"] for op in definitions["gog.email"]["operations"])
