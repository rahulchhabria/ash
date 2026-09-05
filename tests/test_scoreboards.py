from __future__ import annotations

import json
from datetime import date
from io import BytesIO

import pytest

from ash.skills.scoreboards import (
    ScoreboardUnavailable,
    espn_event_to_nba_game,
    fetch_espn_scoreboard,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


def test_fetch_espn_scoreboard_extracts_data(monkeypatch) -> None:
    payload = {"content": {"sbData": {"events": [{"id": "1"}]}}}
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response(payload)

    monkeypatch.setattr("ash.skills.scoreboards.urlopen", fake_urlopen)

    result = fetch_espn_scoreboard("wnba", date(2026, 8, 30), timeout=7)

    assert result["events"] == [{"id": "1"}]
    assert "dates=20260830" in captured["url"]
    assert captured["timeout"] == 7


def test_fetch_espn_scoreboard_rejects_missing_data(monkeypatch) -> None:
    monkeypatch.setattr(
        "ash.skills.scoreboards.urlopen", lambda *args, **kwargs: _Response({})
    )

    with pytest.raises(ScoreboardUnavailable, match="missing scoreboard"):
        fetch_espn_scoreboard("nba", date(2026, 8, 30))


def test_espn_event_to_nba_game_preserves_live_state() -> None:
    event = {
        "id": "401",
        "date": "2026-08-30T19:00Z",
        "competitions": [
            {
                "broadcast": "NBA TV",
                "status": {
                    "period": 4,
                    "displayClock": "3:21",
                    "type": {"id": "2", "detail": "3:21 - 4th Quarter"},
                },
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "88",
                        "team": {
                            "location": "Los Angeles",
                            "name": "Lakers",
                            "abbreviation": "LAL",
                        },
                    },
                    {
                        "homeAway": "away",
                        "score": "86",
                        "team": {
                            "location": "Golden State",
                            "name": "Warriors",
                            "abbreviation": "GSW",
                        },
                    },
                ],
            }
        ],
    }

    game = espn_event_to_nba_game(event)

    assert game is not None
    assert game["gameId"] == "401"
    assert game["gameStatus"] == "2"
    assert game["period"] == 4
    assert game["gameClock"] == "3:21"
    assert game["homeTeam"]["teamTricode"] == "LAL"
    assert game["awayTeam"]["score"] == "86"
    assert (
        game["broadcasters"]["nationalTvBroadcasters"][0]["broadcasterDisplay"]
        == "NBA TV"
    )
