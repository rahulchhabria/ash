#!/usr/bin/env python3
"""WNBA close-game helper for Ash skills."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ash.skills.scoreboards import fetch_espn_scoreboard

WNBA_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
WNBA_CDN_SCHEDULE_URL = "https://cdn.wnba.com/static/json/staticData/scheduleLeagueV2.json"
DEFAULT_TIMEZONE = "America/Los_Angeles"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "state.json"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "config.json"
FETCH_TIMEOUT_SECONDS = 12
CLOCK_TEXT_RE = re.compile(
    r"(?:(\d{1,2}:\d{2})\s*-\s*)?(?:(\d+)(?:st|nd|rd|th|OT)\s+Quarter|(\d+)(?:st|nd|rd|th)|OT)",
    re.IGNORECASE,
)


@dataclass
class TeamView:
    game_id: str
    team_display: str
    team_abbrev: str
    score: int
    opponent_display: str
    opponent_abbrev: str
    opponent_score: int
    preferred_broadcaster: str
    game_status: int
    game_status_text: str
    game_datetime_utc: datetime
    period: int
    clock: str


@dataclass
class TeamGame:
    game_id: str
    team_query: str
    team_display: str
    team_abbrev: str
    opponent_display: str
    opponent_abbrev: str
    preferred_broadcaster: str
    game_datetime_utc: datetime
    game_status: int
    game_status_text: str
    local_date: date


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"teams": [], "alerts": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"teams": [], "alerts": {}}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _canonical(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _to_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _fetch_json(url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    request = Request(  # noqa: S310 - callers use fixed HTTPS endpoints.
        full_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.wnba.com/",
        },
    )
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            content_type = response.headers.get("content-type", "")
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from WNBA endpoint: {full_url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error reaching WNBA endpoint: {exc.reason}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        content_hint = content_type or "unknown content-type"
        raise RuntimeError(
            f"Non-JSON response from WNBA endpoint ({content_hint}): {full_url}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected payload shape from {full_url}")
    return payload


def _telegram_credentials() -> tuple[str, str]:
    config = _load_config()
    token = str(os.getenv("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token") or "").strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id") or "").strip()
    return token, chat_id


def _youtube_tv_url() -> str:
    config = _load_config()
    return str(os.getenv("YOUTUBE_TV_URL") or config.get("youtube_tv_url") or "").strip()


SKILL_NAME = "valkyries-close-game-alert"


def _ash_chat_history_path(chat_id: str) -> Path:
    base = os.environ.get("ASH_HOME")
    home = Path(base).expanduser() if base else Path.home() / ".ash"
    return home / "chats" / "telegram" / chat_id / "history.jsonl"


def _record_alert_in_chat_history(
    chat_id: str,
    text: str,
    *,
    external_id: str | None = None,
) -> None:
    """Append the outbound alert to Ash's chat-level history.

    Lets the close_game_alert integration find the alert when the user
    sends a follow-up message, so the agent has context for the
    conversation. Failures are swallowed; the daemon must keep running
    even if the chat dir is unavailable.
    """
    try:
        import uuid
        history_path = _ash_chat_history_path(chat_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": text,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "metadata": {"source": SKILL_NAME},
        }
        if external_id:
            entry["metadata"]["external_id"] = external_id
        with history_path.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: failed to record alert in chat history: {exc}")


async def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = Request(  # noqa: S310 - Telegram HTTPS endpoint.
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ash-close-game-alert/1.0",
        },
        method="POST",
    )

    def _post() -> str | None:
        with urlopen(request, timeout=20) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, dict):
            message_id = result.get("message_id")
            if message_id is not None:
                return str(message_id)
        return None

    external_id = await asyncio.to_thread(_post)
    _record_alert_in_chat_history(chat_id, text, external_id=external_id)


def _parse_datetime_utc(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def _parse_datetime_maybe_local(raw: str, tz: ZoneInfo) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def _local_date(dt_utc: datetime, tz: ZoneInfo) -> date:
    return dt_utc.astimezone(tz).date()


def _resolve_target_date(date_arg: str | None, tz: ZoneInfo) -> date:
    if not date_arg:
        return datetime.now(tz).date()
    try:
        return datetime.strptime(date_arg, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("Date must be YYYYMMDD") from exc


def _scoreboard_for_date(day: date) -> dict[str, Any]:
    try:
        return _fetch_json(
            WNBA_SCOREBOARD_URL,
            params={"dates": day.strftime("%Y%m%d")},
        )
    except RuntimeError as exc:
        print(f"WARN: ESPN site scoreboard unavailable ({exc}); using CDN fallback.")
        return fetch_espn_scoreboard("wnba", day)


def _events_for_date(day: date) -> list[dict[str, Any]]:
    payload = _scoreboard_for_date(day)
    events = payload.get("events") or []
    return [event for event in events if isinstance(event, dict)]


def _season_calendar_dates() -> list[date]:
    try:
        payload = _fetch_json(WNBA_SCOREBOARD_URL)
    except RuntimeError as exc:
        state = _load_state()
        cached_dates: list[date] = []
        for entries in (state.get("schedule") or {}).values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw = str(entry.get("game_datetime_utc") or "")
                if raw:
                    cached_dates.append(_parse_datetime_utc(raw).date())
        if cached_dates:
            print(
                f"WARN: WNBA calendar unavailable ({exc}); using cached schedule dates."
            )
            return sorted(set(cached_dates))
        raise
    leagues = payload.get("leagues") or []
    if not leagues:
        return []
    calendar = leagues[0].get("calendar") or []
    dates: list[date] = []
    for item in calendar:
        if not isinstance(item, str):
            continue
        try:
            dates.append(datetime.fromisoformat(item.replace("Z", "+00:00")).date())
        except ValueError:
            continue
    return sorted(set(dates))


def _team_game_from_cache_entry(
    entry: dict[str, Any], team_query: str, tz: ZoneInfo
) -> TeamGame | None:
    team_display = str(entry.get("team_display") or entry.get("team") or team_query).strip()
    opponent_display = str(entry.get("opponent_display") or entry.get("opponent") or "").strip()
    if not opponent_display:
        return None
    aliases = {
        _canonical(team_query),
        _canonical(team_display),
        _canonical(str(entry.get("team_abbrev") or "")),
    }
    if _canonical(team_query) not in aliases:
        return None
    dt_utc = _parse_datetime_maybe_local(
        str(entry.get("game_datetime_utc") or entry.get("game_datetime") or ""),
        tz,
    )
    return TeamGame(
        game_id=str(entry.get("game_id") or ""),
        team_query=team_query,
        team_display=team_display,
        team_abbrev=str(entry.get("team_abbrev") or "GS"),
        opponent_display=opponent_display,
        opponent_abbrev=str(entry.get("opponent_abbrev") or "UNK"),
        preferred_broadcaster=str(entry.get("preferred_broadcaster") or ""),
        game_datetime_utc=dt_utc,
        game_status=_to_int(entry.get("game_status")),
        game_status_text=str(entry.get("game_status_text") or "Scheduled"),
        local_date=_local_date(dt_utc, tz),
    )


def _team_view_from_cached_game(game: TeamGame) -> TeamView:
    return TeamView(
        game_id=game.game_id,
        team_display=game.team_display,
        team_abbrev=game.team_abbrev,
        score=0,
        opponent_display=game.opponent_display,
        opponent_abbrev=game.opponent_abbrev,
        opponent_score=0,
        preferred_broadcaster=game.preferred_broadcaster,
        game_status=game.game_status,
        game_status_text=game.game_status_text,
        game_datetime_utc=game.game_datetime_utc,
        period=0,
        clock="0:00",
    )


def _cached_games_for_team(
    team: str, *, now_utc: datetime, tz: ZoneInfo, limit: int
) -> list[TeamGame]:
    state = _load_state()
    schedule = state.get("schedule") or {}
    entries = schedule.get(team) or schedule.get(_canonical(team)) or []
    if not isinstance(entries, list):
        return []
    games: list[TeamGame] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        game = _team_game_from_cache_entry(item, team, tz)
        if game is None:
            continue
        if game.game_datetime_utc < now_utc - timedelta(hours=6):
            continue
        games.append(game)
    return sorted(games, key=lambda item: item.game_datetime_utc)[:limit]


def _cache_covers_date(team: str, target_date: date, tz: ZoneInfo) -> bool:
    state = _load_state()
    games = _cached_games_for_team(
        team,
        now_utc=datetime.combine(target_date, datetime.min.time(), tzinfo=tz).astimezone(UTC)
        - timedelta(days=1),
        tz=tz,
        limit=128,
    )
    if not games:
        return False
    dates = [game.local_date for game in games]
    cache_start = min(dates)
    cached_at_raw = str(state.get("schedule_cached_at_utc") or "")
    if cached_at_raw:
        cache_start = min(cache_start, _parse_datetime_utc(cached_at_raw).astimezone(tz).date())
    return cache_start <= target_date <= max(dates)


def _upsert_cached_games(team: str, games: list[TeamGame]) -> None:
    if not games:
        return
    state = _load_state()
    schedule = state.setdefault("schedule", {})
    schedule[team] = [
        {
            "game_id": game.game_id,
            "team_display": game.team_display,
            "team_abbrev": game.team_abbrev,
            "opponent_display": game.opponent_display,
            "opponent_abbrev": game.opponent_abbrev,
            "preferred_broadcaster": game.preferred_broadcaster,
            "game_datetime_utc": game.game_datetime_utc.isoformat(),
            "game_status": game.game_status,
            "game_status_text": game.game_status_text,
        }
        for game in games
    ]
    state["schedule_cached_at_utc"] = datetime.now(UTC).isoformat()
    _save_state(state)


def _team_aliases(team: dict[str, Any]) -> set[str]:
    location = str(team.get("location") or "").strip()
    name = str(team.get("name") or "").strip()
    abbreviation = str(team.get("abbreviation") or "").strip()
    display = str(team.get("displayName") or "").strip()
    short = str(team.get("shortDisplayName") or "").strip()
    aliases = {location, name, abbreviation, display, short}
    return {_canonical(value) for value in aliases if value}


def _competition_from_event(event: dict[str, Any]) -> dict[str, Any]:
    competitions = event.get("competitions") or []
    if not competitions or not isinstance(competitions[0], dict):
        return {}
    return competitions[0]


def _preferred_broadcaster(event: dict[str, Any], competition: dict[str, Any]) -> str:
    broadcasts = competition.get("broadcasts") or event.get("broadcasts") or []
    if isinstance(broadcasts, list):
        for item in broadcasts:
            if isinstance(item, dict):
                names = item.get("names")
                if isinstance(names, list) and names:
                    return str(names[0]).strip()
                display = str(item.get("displayName") or item.get("shortName") or "").strip()
                if display:
                    return display
            elif isinstance(item, str) and item.strip():
                return item.strip()
    broadcast = str(competition.get("broadcast") or event.get("broadcast") or "").strip()
    return broadcast


def _parse_status(competition: dict[str, Any], event: dict[str, Any]) -> tuple[int, str, int, str]:
    status = competition.get("status") or event.get("status") or {}
    status_type = status.get("type") or {}
    game_status = _to_int(status_type.get("id"))
    detail = str(status_type.get("detail") or status_type.get("shortDetail") or status_type.get("description") or "Status unavailable").strip()
    period = _to_int(status.get("period"))
    clock = str(status.get("displayClock") or "").strip()
    if not clock or clock == "0:00":
        parsed_period, parsed_clock = _parse_period_clock_from_status_text(detail)
        if not period:
            period = parsed_period
        clock = parsed_clock
    return game_status, detail, period, clock


def _parse_period_clock_from_status_text(text: str) -> tuple[int, str]:
    match = CLOCK_TEXT_RE.search(text)
    if not match:
        return 0, "0:00"
    clock = match.group(1) or "0:00"
    period = _to_int(match.group(2) or match.group(3))
    if "ot" in text.lower() and not period:
        period = 5
    return period, clock


def _team_view_from_event(event: dict[str, Any], team_query: str) -> TeamView | None:
    competition = _competition_from_event(event)
    competitors = competition.get("competitors") or []
    if not isinstance(competitors, list):
        return None

    needle = _canonical(team_query)
    selected: dict[str, Any] | None = None
    opponent: dict[str, Any] | None = None
    for competitor in competitors:
        if not isinstance(competitor, dict):
            continue
        team = competitor.get("team") or {}
        if needle in _team_aliases(team):
            selected = competitor
        else:
            opponent = competitor
    if selected is None or opponent is None:
        return None

    selected_team = selected.get("team") or {}
    opponent_team = opponent.get("team") or {}
    game_status, detail, period, clock = _parse_status(competition, event)
    raw_date = str(competition.get("date") or event.get("date") or "")
    game_dt = _parse_datetime_utc(raw_date)

    return TeamView(
        game_id=str(event.get("id") or competition.get("id") or ""),
        team_display=str(selected_team.get("displayName") or ""),
        team_abbrev=str(selected_team.get("abbreviation") or "UNK"),
        score=_to_int(selected.get("score")),
        opponent_display=str(opponent_team.get("displayName") or ""),
        opponent_abbrev=str(opponent_team.get("abbreviation") or "UNK"),
        opponent_score=_to_int(opponent.get("score")),
        preferred_broadcaster=_preferred_broadcaster(event, competition),
        game_status=game_status,
        game_status_text=detail,
        game_datetime_utc=game_dt,
        period=period,
        clock=clock or "0:00",
    )


def _team_game_from_event(event: dict[str, Any], team_query: str, tz: ZoneInfo) -> TeamGame | None:
    view = _team_view_from_event(event, team_query)
    if view is None:
        return None
    return TeamGame(
        game_id=view.game_id,
        team_query=team_query,
        team_display=view.team_display,
        team_abbrev=view.team_abbrev,
        opponent_display=view.opponent_display,
        opponent_abbrev=view.opponent_abbrev,
        preferred_broadcaster=view.preferred_broadcaster,
        game_datetime_utc=view.game_datetime_utc,
        game_status=view.game_status,
        game_status_text=view.game_status_text,
        local_date=_local_date(view.game_datetime_utc, tz),
    )


def _games_for_team_on_date(team: str, target_date: date, tz: ZoneInfo) -> list[TeamView]:
    now_utc = datetime.now(UTC)
    cached = [
        _team_view_from_cached_game(game)
        for game in _cached_games_for_team(team, now_utc=now_utc - timedelta(days=1), tz=tz, limit=64)
        if game.local_date == target_date
    ]
    is_today = target_date == now_utc.astimezone(tz).date()
    if cached and not is_today:
        return cached
    if not cached and _cache_covers_date(team, target_date, tz):
        return []

    matches: list[TeamView] = []
    for event in _events_for_date(target_date):
        view = _team_view_from_event(event, team)
        if view is None:
            continue
        if _local_date(view.game_datetime_utc, tz) != target_date:
            continue
        matches.append(view)
    return matches or cached


def _select_live_or_first(games: list[TeamView]) -> TeamView:
    for game in games:
        if game.game_status == 2:
            return game
    return games[0]


def _clock_seconds(clock: str) -> int:
    parts = clock.strip().split(":")
    if len(parts) != 2:
        return 0
    try:
        minutes = int(parts[0])
        seconds = int(parts[1])
    except ValueError:
        return 0
    return max(minutes, 0) * 60 + max(seconds, 0)


def _remaining_games_for_team(
    team: str,
    *,
    now_utc: datetime,
    tz: ZoneInfo,
    limit: int = 32,
    prefer_cache: bool = True,
) -> list[TeamGame]:
    if prefer_cache:
        cached = _cached_games_for_team(team, now_utc=now_utc, tz=tz, limit=limit)
        if cached:
            return cached

    matches: list[TeamGame] = []
    for season_day in _season_calendar_dates():
        if season_day < now_utc.astimezone(tz).date() - timedelta(days=1):
            continue
        for event in _events_for_date(season_day):
            game = _team_game_from_event(event, team, tz)
            if game is None:
                continue
            if game.game_status >= 3 and game.game_datetime_utc < now_utc + timedelta(minutes=5):
                continue
            if game.game_datetime_utc < now_utc - timedelta(hours=6):
                continue
            matches.append(game)
            if len(matches) >= limit:
                sorted_matches = sorted(matches, key=lambda item: item.game_datetime_utc)
                _upsert_cached_games(team, sorted_matches)
                return sorted_matches
    sorted_matches = sorted(matches, key=lambda item: item.game_datetime_utc)
    _upsert_cached_games(team, sorted_matches)
    return sorted_matches


def cmd_set_teams(args: argparse.Namespace) -> int:
    state = _load_state()
    teams = [team.strip() for team in args.team if team.strip()]
    unique = list(dict.fromkeys(teams))
    state["teams"] = unique
    _save_state(state)
    print(f"Saved {len(unique)} team(s): {', '.join(unique) if unique else '(none)'}")
    return 0


def cmd_refresh_schedule(args: argparse.Namespace) -> int:
    state = _load_state()
    teams = [str(team) for team in (state.get("teams") or []) if str(team).strip()]
    if args.team:
        teams = [args.team]
    if not teams:
        print("No team specified and no followed teams in state. Use set-teams or --team.")
        return 1

    tz = ZoneInfo(args.tz)
    start = _resolve_target_date(args.start_date, tz)
    end = start + timedelta(days=max(args.days - 1, 0))

    schedule_cache: dict[str, list[dict[str, str]]] = {}
    for team in teams:
        schedule_cache[team] = []
        for day in _season_calendar_dates():
            if day < start or day > end:
                continue
            for event in _events_for_date(day):
                view = _team_view_from_event(event, team)
                if view is None:
                    continue
                local_day = _local_date(view.game_datetime_utc, tz)
                if not (start <= local_day <= end):
                    continue
                schedule_cache[team].append(
                    {
                        "game_id": view.game_id,
                        "team_display": view.team_display,
                        "team_abbrev": view.team_abbrev,
                        "opponent_display": view.opponent_display,
                        "opponent_abbrev": view.opponent_abbrev,
                        "preferred_broadcaster": view.preferred_broadcaster,
                        "game_datetime_utc": view.game_datetime_utc.isoformat(),
                        "game_status": view.game_status,
                        "game_status_text": view.game_status_text,
                    }
                )

    state["schedule"] = schedule_cache
    state["schedule_cached_at_utc"] = datetime.now(UTC).isoformat()
    state["schedule_timezone"] = args.tz
    _save_state(state)

    print(
        f"Cached schedule for {len(schedule_cache)} team(s) "
        f"from {start.isoformat()} to {end.isoformat()} ({args.tz})."
    )
    for team, entries in schedule_cache.items():
        print(f"- {team}: {len(entries)} game(s)")
    return 0


def cmd_list_teams(_: argparse.Namespace) -> int:
    teams = _load_state().get("teams") or []
    if not teams:
        print("No followed teams set. Use set-teams first.")
        return 1
    print("Followed teams:")
    for team in teams:
        print(f"- {team}")
    return 0


def cmd_today(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.tz)
    target_date = _resolve_target_date(args.date, tz)
    games = _games_for_team_on_date(args.team, target_date, tz)
    if not games:
        print(f"NO: {args.team} is not playing on {target_date.isoformat()} ({args.tz}).")
        return 0

    game = _select_live_or_first(games)
    print(f"YES: {game.team_display} is playing on {target_date.isoformat()} ({args.tz}).")
    print(f"MATCHUP: {game.team_abbrev} vs {game.opponent_abbrev}")
    print(f"STATUS: {game.game_status_text}")
    return 0


def cmd_game_status(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.tz)
    target_date = _resolve_target_date(args.date, tz)
    games = _games_for_team_on_date(args.team, target_date, tz)
    if not games:
        print(f"No game found for {args.team} on {target_date.isoformat()} ({args.tz}).")
        return 1

    view = _select_live_or_first(games)
    margin = view.score - view.opponent_score
    state = "tied"
    if margin > 0:
        state = "winning"
    elif margin < 0:
        state = "losing"

    print(f"TEAM: {view.team_display} ({view.team_abbrev})")
    print(f"OPPONENT: {view.opponent_display} ({view.opponent_abbrev})")
    print(
        f"SCORE: {view.team_abbrev} {view.score} - "
        f"{view.opponent_abbrev} {view.opponent_score}"
    )
    print(f"GAME_STATE: {state}")
    print(f"MARGIN: {margin}")
    print(f"STATUS: {view.game_status_text} (Q{view.period}, {view.clock})")
    if view.game_id:
        print(f"GAME_ID: {view.game_id}")
    return 0


def _evaluate_alert(
    view: TeamView, *, threshold: int, minutes_left: int
) -> tuple[bool, str]:
    in_target_period = view.period == 4
    secs_left = _clock_seconds(view.clock)
    in_window = 0 < secs_left <= minutes_left * 60
    delta = abs(view.score - view.opponent_score)
    close = delta <= threshold
    should_alert = in_target_period and in_window and close
    message = (
        f"{view.team_display} vs {view.opponent_display} | "
        f"score {view.team_abbrev} {view.score}-{view.opponent_score} {view.opponent_abbrev} | "
        f"delta={delta} | Q{view.period} {view.clock} | {view.game_status_text}"
    )
    return should_alert, message


def cmd_alert_scan(args: argparse.Namespace) -> int:
    state = _load_state()
    teams = (
        [args.team]
        if args.team
        else [str(team) for team in (state.get("teams") or []) if str(team).strip()]
    )
    if not teams:
        print("No team specified and no followed teams in state. Use set-teams or --team.")
        return 1

    tz = ZoneInfo(args.tz)
    target_date = _resolve_target_date(args.date, tz)
    alerts = state.setdefault("alerts", {})
    emitted = 0

    for team in teams:
        games = _games_for_team_on_date(team, target_date, tz)
        if not games:
            print(f"INFO: {team} has no game on {target_date.isoformat()} ({args.tz}).")
            continue

        view = _select_live_or_first(games)
        should_alert, detail = _evaluate_alert(
            view,
            threshold=args.threshold,
            minutes_left=args.minutes_left,
        )
        game_id = view.game_id or f"{target_date.isoformat()}:{view.team_abbrev}:{view.opponent_abbrev}"
        prior = alerts.get(game_id, {})
        outcome = "close" if should_alert else "not_close"
        if prior.get("outcome") == outcome:
            print(f"INFO: No state change for {team} ({outcome}).")
            continue

        alerts[game_id] = {
            "team": view.team_abbrev,
            "opponent": view.opponent_abbrev,
            "outcome": outcome,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        }

        if should_alert:
            emitted += 1
            print(f"ALERT: CLOSE GAME -> {detail}")
        elif args.notify_non_close:
            emitted += 1
            print(f"ALERT: NOT CLOSE -> {detail}")
        else:
            print(f"INFO: Not close -> {detail}")

    _save_state(state)
    print(f"ALERT_COUNT: {emitted}")
    return 0


def cmd_upcoming(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.tz)
    now_utc = datetime.now(UTC)
    games = _remaining_games_for_team(
        args.team,
        now_utc=now_utc,
        tz=tz,
        limit=max(args.limit, 1) * 2,
        prefer_cache=not args.refresh,
    )
    if args.limit > 0:
        games = games[: args.limit]
    if not games:
        print(f"No upcoming games found for {args.team}.")
        return 0

    print(f"Upcoming games for {args.team}:")
    for game in games:
        local_dt = game.game_datetime_utc.astimezone(tz)
        print(
            f"- {local_dt.strftime('%a %Y-%m-%d %H:%M %Z')} | "
            f"{game.team_abbrev} vs {game.opponent_abbrev} | game_id={game.game_id}"
        )
    return 0


def _format_close_alert(view: TeamView) -> str:
    team_name = view.team_display.split()[-1] if view.team_display else view.team_abbrev
    opponent_name = view.opponent_display.split()[-1] if view.opponent_display else view.opponent_abbrev
    lines = [
        "Close Game Alert\n"
        f"{team_name} vs {opponent_name}\n"
        f"{view.team_abbrev} {view.score} - {view.opponent_score} {view.opponent_abbrev}\n"
        f"Q{view.period} {view.clock}"
    ]
    youtube_tv_url = _youtube_tv_url()
    if youtube_tv_url and view.preferred_broadcaster:
        lines.append(f"Watch: {view.preferred_broadcaster} on YouTube TV")
        lines.append(youtube_tv_url)
    elif youtube_tv_url:
        lines.append(f"Watch: {youtube_tv_url}")
    elif view.preferred_broadcaster:
        lines.append(f"Watch: {view.preferred_broadcaster}")
    return "\n".join(lines)


async def _monitor_game(
    *,
    team: str,
    game: TeamGame,
    tz: ZoneInfo,
    threshold: int,
    minutes_left: int,
    poll_seconds: int,
    quiet_prestart_seconds: int,
    dry_run: bool,
) -> int:
    state = _load_state()
    alerts = state.setdefault("alerts", {})
    token, chat_id = _telegram_credentials()
    if not dry_run and (not token or not chat_id):
        print(
            "Telegram destination missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "or populate valkyries-close-game-alert/data/config.json."
        )
        return 1

    print(
        f"Watching {game.team_abbrev} vs {game.opponent_abbrev} "
        f"starting {game.game_datetime_utc.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}"
    )

    game_date_arg = game.local_date.strftime("%Y%m%d")
    fetch_failures = 0
    while True:
        now_utc = datetime.now(UTC)
        lead_seconds = int((game.game_datetime_utc - now_utc).total_seconds())
        if lead_seconds > quiet_prestart_seconds:
            sleep_for = max(min(lead_seconds - quiet_prestart_seconds, 1800), 60)
            print(f"INFO: Sleeping {sleep_for}s until pregame window.")
            await asyncio.sleep(sleep_for)
            continue

        try:
            games = _games_for_team_on_date(team, _resolve_target_date(game_date_arg, tz), tz)
            fetch_failures = 0
        except RuntimeError as exc:
            fetch_failures += 1
            sleep_for = min(max(poll_seconds, 300) * min(fetch_failures, 6), 1800)
            if fetch_failures == 1 or fetch_failures % 6 == 0:
                print(
                    f"WARN: Live scoreboard unavailable: {exc}. "
                    f"Sleeping {sleep_for}s before retry."
                )
            await asyncio.sleep(sleep_for)
            continue
        if not games:
            print(f"INFO: No scheduled game found for {team} on {game_date_arg}.")
            return 1

        view = _select_live_or_first(games)
        if view.game_status >= 3 or "final" in view.game_status_text.lower():
            alerts[view.game_id or game.game_id] = {
                "team": view.team_abbrev,
                "opponent": view.opponent_abbrev,
                "outcome": "final",
                "updated_at_utc": datetime.now(UTC).isoformat(),
            }
            _save_state(state)
            print(f"FINAL: {view.game_status_text}")
            return 0

        if view.game_status < 2 and lead_seconds > 0:
            sleep_for = max(min(lead_seconds, poll_seconds), 30)
            print(f"INFO: Game not live yet ({view.game_status_text or 'pregame'}). Sleeping {sleep_for}s.")
            await asyncio.sleep(sleep_for)
            continue

        should_alert, detail = _evaluate_alert(
            view,
            threshold=threshold,
            minutes_left=minutes_left,
        )
        game_key = view.game_id or game.game_id
        prior = alerts.get(game_key, {})
        outcome = "close" if should_alert else "not_close"

        if should_alert and prior.get("outcome") != "close":
            message = _format_close_alert(view)
            if dry_run:
                print(f"ALERT: {message}")
            else:
                await _send_telegram(token, chat_id, message)
                print(f"ALERT_SENT: {message}")

        alerts[game_key] = {
            "team": view.team_abbrev,
            "opponent": view.opponent_abbrev,
            "outcome": outcome,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        }
        _save_state(state)
        print(f"INFO: {detail}")
        await asyncio.sleep(max(poll_seconds, 30))


def cmd_watch_game(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.tz)
    now_utc = datetime.now(UTC)

    target_game: TeamGame | None = None
    remaining = _remaining_games_for_team(args.team, now_utc=now_utc - timedelta(days=2), tz=tz, limit=64)
    if args.game_id:
        for game in remaining:
            if game.game_id == args.game_id:
                target_game = game
                break
    if target_game is None:
        if args.date:
            wanted = _resolve_target_date(args.date, tz)
            for game in remaining:
                if game.local_date == wanted:
                    target_game = game
                    break
        else:
            future = [game for game in remaining if game.game_datetime_utc >= now_utc - timedelta(hours=6)]
            if future:
                target_game = future[0]

    if target_game is None:
        print(f"No upcoming game found for {args.team}.")
        return 1

    return asyncio.run(
        _monitor_game(
            team=args.team,
            game=target_game,
            tz=tz,
            threshold=args.threshold,
            minutes_left=args.minutes_left,
            poll_seconds=args.poll_seconds,
            quiet_prestart_seconds=args.quiet_prestart_seconds,
            dry_run=args.dry_run,
        )
    )


async def _daemon_loop(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.tz)
    team = args.team
    print(f"Starting close-game-alert daemon for {team} ({args.tz}).")
    fetch_failures = 0

    while True:
        now_utc = datetime.now(UTC)
        try:
            remaining = _remaining_games_for_team(team, now_utc=now_utc, tz=tz, limit=16)
            fetch_failures = 0
        except RuntimeError as exc:
            fetch_failures += 1
            sleep_for = max(args.idle_poll_seconds, 300)
            if fetch_failures == 1 or fetch_failures % 6 == 0:
                print(f"WARN: Error fetching WNBA data: {exc}. Sleeping {sleep_for}s before retry.")
            await asyncio.sleep(sleep_for)
            continue
        if not remaining:
            sleep_for = max(args.idle_poll_seconds, 900)
            print(f"INFO: No remaining games found for {team}. Sleeping {sleep_for}s.")
            await asyncio.sleep(sleep_for)
            continue

        next_game = remaining[0]
        lead_seconds = int((next_game.game_datetime_utc - now_utc).total_seconds())
        if lead_seconds > args.quiet_prestart_seconds:
            sleep_for = max(
                min(lead_seconds - args.quiet_prestart_seconds, args.idle_poll_seconds),
                60,
            )
            local_dt = next_game.game_datetime_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
            print(
                f"INFO: Next game {next_game.team_abbrev} vs {next_game.opponent_abbrev} "
                f"at {local_dt}. Sleeping {sleep_for}s."
            )
            await asyncio.sleep(sleep_for)
            continue

        result = await _monitor_game(
            team=team,
            game=next_game,
            tz=tz,
            threshold=args.threshold,
            minutes_left=args.minutes_left,
            poll_seconds=args.poll_seconds,
            quiet_prestart_seconds=args.quiet_prestart_seconds,
            dry_run=args.dry_run,
        )
        if result != 0:
            await asyncio.sleep(max(args.idle_poll_seconds, 300))


def cmd_daemon(args: argparse.Namespace) -> int:
    return asyncio.run(_daemon_loop(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WNBA close-game alert helper")
    sub = parser.add_subparsers(dest="command", required=True)

    set_teams = sub.add_parser("set-teams", help="Set followed team list")
    set_teams.add_argument("--team", action="append", required=True, help="Team to follow")
    set_teams.set_defaults(func=cmd_set_teams)

    refresh = sub.add_parser("refresh-schedule", help="Cache upcoming schedule for teams")
    refresh.add_argument("--team", help="Team to cache (default: all followed teams)")
    refresh.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of days to cache starting from start-date",
    )
    refresh.add_argument("--start-date", help="Start date as YYYYMMDD (default: today in timezone)")
    refresh.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    refresh.set_defaults(func=cmd_refresh_schedule)

    list_teams = sub.add_parser("list-teams", help="List followed teams")
    list_teams.set_defaults(func=cmd_list_teams)

    today = sub.add_parser("today", help="Check if team is playing today")
    today.add_argument("--team", required=True, help="Team name, city, nickname, or abbreviation")
    today.add_argument("--date", help="Date as YYYYMMDD (default: today in timezone)")
    today.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    today.set_defaults(func=cmd_today)

    status = sub.add_parser("game-status", help="Show team score and winning or losing state")
    status.add_argument("--team", required=True, help="Team name, city, nickname, or abbreviation")
    status.add_argument("--date", help="Date as YYYYMMDD (default: today in timezone)")
    status.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    status.set_defaults(func=cmd_game_status)

    scan = sub.add_parser("alert-scan", help="Scan games and emit close-game alerts")
    scan.add_argument("--team", help="Team to scan (default: all followed teams)")
    scan.add_argument("--date", help="Date as YYYYMMDD (default: today in timezone)")
    scan.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    scan.add_argument("--threshold", type=int, default=6, help="Max score delta for close game")
    scan.add_argument("--minutes-left", type=int, default=5, help="Minutes remaining threshold in Q4")
    scan.add_argument(
        "--notify-non-close",
        action="store_true",
        help="Emit alerts for non-close outcomes too (state-change only)",
    )
    scan.set_defaults(func=cmd_alert_scan)

    upcoming = sub.add_parser("upcoming", help="List remaining scheduled games")
    upcoming.add_argument("--team", required=True, help="Team name, city, nickname, or abbreviation")
    upcoming.add_argument("--limit", type=int, default=12, help="Number of upcoming games to show")
    upcoming.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass local schedule cache and refresh from the remote source",
    )
    upcoming.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    upcoming.set_defaults(func=cmd_upcoming)

    watch = sub.add_parser(
        "watch-game",
        help="Wait for tipoff and poll only during a specific upcoming game",
    )
    watch.add_argument("--team", required=True, help="Team name, city, nickname, or abbreviation")
    watch.add_argument("--game-id", help="Specific game id to watch")
    watch.add_argument("--date", help="Local game date as YYYYMMDD")
    watch.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    watch.add_argument("--threshold", type=int, default=6, help="Max score delta for close game")
    watch.add_argument("--minutes-left", type=int, default=5, help="Minutes remaining threshold in Q4")
    watch.add_argument("--poll-seconds", type=int, default=120, help="Scoreboard poll interval during game")
    watch.add_argument(
        "--quiet-prestart-seconds",
        type=int,
        default=900,
        help="Start checking this many seconds before listed tipoff",
    )
    watch.add_argument("--dry-run", action="store_true", help="Print alerts without Telegram")
    watch.set_defaults(func=cmd_watch_game)

    daemon = sub.add_parser(
        "daemon",
        help="Long-running watcher that sleeps until tipoff, then polls during games only",
    )
    daemon.add_argument("--team", required=True, help="Team name, city, nickname, or abbreviation")
    daemon.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    daemon.add_argument("--threshold", type=int, default=6, help="Max score delta for close game")
    daemon.add_argument("--minutes-left", type=int, default=5, help="Minutes remaining threshold in Q4")
    daemon.add_argument("--poll-seconds", type=int, default=120, help="Scoreboard poll interval during games")
    daemon.add_argument(
        "--idle-poll-seconds",
        type=int,
        default=1800,
        help="How often to refresh the future schedule when no game is near",
    )
    daemon.add_argument(
        "--quiet-prestart-seconds",
        type=int,
        default=900,
        help="Start checking this many seconds before listed tipoff",
    )
    daemon.add_argument("--dry-run", action="store_true", help="Print alerts without Telegram")
    daemon.set_defaults(func=cmd_daemon)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"Error fetching WNBA data: {exc}")
        return 2
    except ValueError as exc:
        print(f"Input error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
