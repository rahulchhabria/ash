"""Free scoreboard fallbacks used by Ash sports skills."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

League = Literal["nba", "wnba"]
ESPN_CDN_SCOREBOARD_URL = "https://cdn.espn.com/core/{league}/scoreboard"
_USER_AGENT = "ash-close-game-alert/3.0"


class ScoreboardUnavailable(RuntimeError):
    """Raised when a fallback scoreboard cannot be fetched or parsed."""


def fetch_espn_scoreboard(
    league: League,
    day: date,
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    """Fetch ESPN's public CDN scoreboard and return its normalized data block."""
    if league not in {"nba", "wnba"}:
        raise ValueError(f"Unsupported basketball league: {league}")
    day_text = day.strftime("%Y%m%d")
    url = (
        ESPN_CDN_SCOREBOARD_URL.format(league=league)
        + f"?xhr=1&limit=100&dates={day_text}"
    )
    request = Request(  # noqa: S310 - URL is built from a fixed HTTPS host.
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ScoreboardUnavailable(
            f"ESPN CDN returned HTTP {exc.code} for {league.upper()}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise ScoreboardUnavailable(
            f"ESPN CDN request failed for {league.upper()}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ScoreboardUnavailable(
            f"ESPN CDN returned invalid JSON for {league.upper()}"
        ) from exc

    if not isinstance(payload, dict):
        raise ScoreboardUnavailable("ESPN CDN returned an unexpected payload")
    content = payload.get("content")
    scoreboard = content.get("sbData") if isinstance(content, dict) else None
    if not isinstance(scoreboard, dict):
        raise ScoreboardUnavailable("ESPN CDN payload is missing scoreboard data")
    events = scoreboard.get("events")
    if not isinstance(events, list):
        raise ScoreboardUnavailable("ESPN CDN scoreboard is missing events")
    return scoreboard


def espn_event_to_nba_game(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an ESPN event into the NBA CDN shape expected by the NBA skill."""
    competitions = event.get("competitions")
    if not isinstance(competitions, list) or not competitions:
        return None
    competition = competitions[0]
    if not isinstance(competition, dict):
        return None
    competitors = competition.get("competitors")
    if not isinstance(competitors, list):
        return None

    sides: dict[str, dict[str, Any]] = {}
    for competitor in competitors:
        if not isinstance(competitor, dict):
            continue
        home_away = str(competitor.get("homeAway") or "")
        team = competitor.get("team")
        if home_away not in {"home", "away"} or not isinstance(team, dict):
            continue
        location = str(team.get("location") or "").strip()
        name = str(team.get("name") or "").strip()
        sides[home_away] = {
            "teamCity": location,
            "teamName": name,
            "teamTricode": str(team.get("abbreviation") or "").strip(),
            "teamSlug": str(team.get("slug") or name).strip().lower().replace(" ", "-"),
            "score": competitor.get("score") or "0",
        }
    if set(sides) != {"home", "away"}:
        return None

    status = competition.get("status") or event.get("status") or {}
    status = status if isinstance(status, dict) else {}
    status_type = status.get("type")
    status_type = status_type if isinstance(status_type, dict) else {}
    detail = str(
        status_type.get("detail")
        or status_type.get("shortDetail")
        or status_type.get("description")
        or "Status unavailable"
    ).strip()
    broadcast = str(competition.get("broadcast") or "").strip()
    broadcasters: dict[str, list[dict[str, str]]] = {}
    if broadcast:
        broadcasters["nationalTvBroadcasters"] = [
            {
                "broadcasterMedia": "tv",
                "broadcasterDisplay": broadcast,
            }
        ]

    return {
        "gameId": str(event.get("id") or competition.get("id") or ""),
        "gameDateTimeUTC": str(competition.get("date") or event.get("date") or ""),
        "gameStatus": status_type.get("id") or 0,
        "gameStatusText": detail,
        "period": status.get("period") or 0,
        "gameClock": str(status.get("displayClock") or "0:00"),
        "homeTeam": sides["home"],
        "awayTeam": sides["away"],
        "broadcasters": broadcasters,
    }
