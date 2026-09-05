#!/usr/bin/env python3
"""NBA close-game helper for Ash skills."""

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
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ash.skills.scoreboards import espn_event_to_nba_game, fetch_espn_scoreboard

NBA_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"
NBA_LIVE_URL = (
    "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
)
DEFAULT_TIMEZONE = "America/Los_Angeles"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "state.json"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "config.json"
CLOCK_TEXT_RE = re.compile(r"Q\s*([1-4])\s+([0-9]{1,2}:[0-9]{2})", re.IGNORECASE)
PT_CLOCK_RE = re.compile(r"PT(?:(\d+)M)?([0-9]+(?:\.[0-9]+)?)S")


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


def _fetch_json(url: str) -> dict[str, Any]:
    request = Request(  # noqa: S310 - callers use fixed HTTPS endpoints.
        url, headers={"User-Agent": "ash-close-game-alert/2.0"}
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from NBA endpoint: {url}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"Network error reaching NBA endpoint: {exc.reason}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected payload shape from {url}")
    return payload


def _telegram_credentials() -> tuple[str, str]:
    config = _load_config()
    token = str(
        os.getenv("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token") or ""
    ).strip()
    chat_id = str(
        os.getenv("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id") or ""
    ).strip()
    return token, chat_id


def _youtube_tv_url() -> str:
    config = _load_config()
    return str(
        os.getenv("YOUTUBE_TV_URL") or config.get("youtube_tv_url") or ""
    ).strip()


def _preferred_broadcaster(
    game: dict[str, Any], *, team_is_home: bool, team_is_away: bool
) -> str:
    broadcasters = game.get("broadcasters") or {}
    if not isinstance(broadcasters, dict):
        return ""

    def _first_tv_display(items: Any) -> str:
        if not isinstance(items, list):
            return ""
        for item in items:
            if not isinstance(item, dict):
                continue
            media = str(item.get("broadcasterMedia") or "").strip().lower()
            if media != "tv":
                continue
            display = str(
                item.get("broadcasterDisplay")
                or item.get("broadcasterAbbreviation")
                or ""
            ).strip()
            if display:
                return display
        return ""

    national = _first_tv_display(broadcasters.get("nationalTvBroadcasters"))
    if national:
        return national
    if team_is_home:
        home = _first_tv_display(broadcasters.get("homeTvBroadcasters"))
        if home:
            return home
    if team_is_away:
        away = _first_tv_display(broadcasters.get("awayTvBroadcasters"))
        if away:
            return away
    return ""


SKILL_NAME = "close-game-alert"


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
            "User-Agent": "ash-close-game-alert/2.1",
        },
        method="POST",
    )

    def _post() -> str | None:
        with urlopen(request, timeout=20) as response:  # noqa: S310  # nosec B310
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


def _parse_game_datetime_utc(game: dict[str, Any]) -> datetime:
    raw = str(game.get("gameDateTimeUTC") or game.get("gameDateUTC") or "")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _espn_games(day: date) -> list[dict[str, Any]]:
    payload = fetch_espn_scoreboard("nba", day)
    games: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        game = espn_event_to_nba_game(event)
        if game is not None:
            games.append(game)
    return games


def _schedule_games(day: date | None = None) -> list[dict[str, Any]]:
    try:
        payload = _fetch_json(NBA_SCHEDULE_URL)
    except RuntimeError as exc:
        target = day or datetime.now(UTC).date()
        print(f"WARN: NBA schedule unavailable ({exc}); using ESPN CDN fallback.")
        return _espn_games(target)
    dates = (payload.get("leagueSchedule") or {}).get("gameDates") or []
    games: list[dict[str, Any]] = []
    for item in dates:
        if not isinstance(item, dict):
            continue
        for game in item.get("games") or []:
            if isinstance(game, dict):
                games.append(game)
    return games


def _live_game_map(day: date | None = None) -> dict[str, dict[str, Any]]:
    try:
        payload = _fetch_json(NBA_LIVE_URL)
    except RuntimeError as exc:
        target = day or datetime.now(UTC).date()
        print(
            f"WARN: NBA live scoreboard unavailable ({exc}); using ESPN CDN fallback."
        )
        return {
            str(game.get("gameId")): game
            for game in _espn_games(target)
            if game.get("gameId")
        }
    games = (payload.get("scoreboard") or {}).get("games") or []
    result: dict[str, dict[str, Any]] = {}
    for game in games:
        if not isinstance(game, dict):
            continue
        gid = str(game.get("gameId") or "")
        if gid:
            result[gid] = game
    return result


def _team_aliases(side: dict[str, Any]) -> set[str]:
    city = str(side.get("teamCity") or "").strip()
    name = str(side.get("teamName") or "").strip()
    tri = str(side.get("teamTricode") or "").strip()
    slug = str(side.get("teamSlug") or "").strip()
    display = f"{city} {name}".strip()
    aliases = {city, name, tri, slug, display}
    return {_canonical(v) for v in aliases if v}


def _team_view_from_game(game: dict[str, Any], team_query: str) -> TeamView | None:
    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    needle = _canonical(team_query)

    home_match = needle in _team_aliases(home)
    away_match = needle in _team_aliases(away)
    if not home_match and not away_match:
        return None

    if home_match:
        team_side, opp_side = home, away
    else:
        team_side, opp_side = away, home

    return TeamView(
        game_id=str(game.get("gameId") or ""),
        team_display=f"{team_side.get('teamCity', '')} {team_side.get('teamName', '')}".strip(),
        team_abbrev=str(team_side.get("teamTricode") or "UNK"),
        score=_to_int(team_side.get("score")),
        opponent_display=f"{opp_side.get('teamCity', '')} {opp_side.get('teamName', '')}".strip(),
        opponent_abbrev=str(opp_side.get("teamTricode") or "UNK"),
        opponent_score=_to_int(opp_side.get("score")),
        preferred_broadcaster=_preferred_broadcaster(
            game, team_is_home=home_match, team_is_away=away_match
        ),
        game_status=_to_int(game.get("gameStatus")),
        game_status_text=str(game.get("gameStatusText") or "Status unavailable"),
        game_datetime_utc=_parse_game_datetime_utc(game),
    )


def _team_game_from_game(game: dict[str, Any], team_query: str) -> TeamGame | None:
    view = _team_view_from_game(game, team_query)
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
    )


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


def _parse_period_clock_from_status_text(text: str) -> tuple[int, str]:
    match = CLOCK_TEXT_RE.search(text)
    if not match:
        return 0, "0:00"
    return _to_int(match.group(1)), match.group(2)


def _parse_pt_clock(clock: str) -> str:
    match = PT_CLOCK_RE.fullmatch(clock.strip())
    if not match:
        return "0:00"
    minutes = _to_int(match.group(1) or 0)
    seconds = int(float(match.group(2)))
    return f"{minutes}:{seconds:02d}"


def _local_date(dt_utc: datetime, tz: ZoneInfo) -> date:
    return dt_utc.astimezone(tz).date()


def _resolve_target_date(date_arg: str | None, tz: ZoneInfo) -> date:
    if not date_arg:
        return datetime.now(tz).date()
    try:
        return datetime.strptime(date_arg, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("Date must be YYYYMMDD") from exc


def _games_for_team_on_date(
    all_games: list[dict[str, Any]],
    team: str,
    target_date: date,
    tz: ZoneInfo,
) -> list[TeamView]:
    matches: list[TeamView] = []
    for game in all_games:
        view = _team_view_from_game(game, team)
        if view is None:
            continue
        if _local_date(view.game_datetime_utc, tz) != target_date:
            continue
        matches.append(view)
    return matches


def _select_live_or_first(games: list[TeamView]) -> TeamView:
    for game in games:
        if game.game_status == 2:
            return game
    return games[0]


def _remaining_games_for_team(
    all_games: list[dict[str, Any]],
    team: str,
    *,
    now_utc: datetime,
) -> list[TeamGame]:
    matches: list[TeamGame] = []
    for game in all_games:
        view = _team_game_from_game(game, team)
        if view is None:
            continue
        if view.game_status >= 3 and view.game_datetime_utc < now_utc + timedelta(
            minutes=5
        ):
            continue
        if view.game_datetime_utc < now_utc - timedelta(hours=6):
            continue
        matches.append(view)
    matches.sort(key=lambda item: item.game_datetime_utc)
    return matches


def _enrich_with_live(
    view: TeamView, live_map: dict[str, dict[str, Any]]
) -> tuple[TeamView, int, str, str]:
    live = live_map.get(view.game_id)
    if not live:
        period, clock = _parse_period_clock_from_status_text(view.game_status_text)
        return view, period, clock, view.game_status_text

    home = live.get("homeTeam") or {}
    away = live.get("awayTeam") or {}
    team_is_home = str(home.get("teamTricode") or "") == view.team_abbrev

    score = _to_int(home.get("score")) if team_is_home else _to_int(away.get("score"))
    opp_score = (
        _to_int(away.get("score")) if team_is_home else _to_int(home.get("score"))
    )

    enriched = TeamView(
        game_id=view.game_id,
        team_display=view.team_display,
        team_abbrev=view.team_abbrev,
        score=score,
        opponent_display=view.opponent_display,
        opponent_abbrev=view.opponent_abbrev,
        opponent_score=opp_score,
        preferred_broadcaster=view.preferred_broadcaster,
        game_status=_to_int(live.get("gameStatus")),
        game_status_text=str(live.get("gameStatusText") or view.game_status_text),
        game_datetime_utc=view.game_datetime_utc,
    )

    period = _to_int(live.get("period"))
    raw_clock = str(live.get("gameClock") or "")
    if raw_clock.startswith("PT"):
        clock = _parse_pt_clock(raw_clock)
    elif ":" in raw_clock:
        clock = raw_clock
    else:
        clock = "0:00"
    if clock == "0:00":
        parsed_period, parsed_clock = _parse_period_clock_from_status_text(
            enriched.game_status_text
        )
        if period == 0:
            period = parsed_period
        clock = parsed_clock

    return enriched, period, clock, enriched.game_status_text


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
        print(
            "No team specified and no followed teams in state. Use set-teams or --team."
        )
        return 1

    tz = ZoneInfo(args.tz)
    start = _resolve_target_date(args.start_date, tz)
    end = start + timedelta(days=max(args.days - 1, 0))
    all_games = _schedule_games(start)

    schedule_cache: dict[str, list[dict[str, str]]] = {}
    for team in teams:
        schedule_cache[team] = []
        for game in all_games:
            view = _team_view_from_game(game, team)
            if view is None:
                continue
            local_day = _local_date(view.game_datetime_utc, tz)
            if not (start <= local_day <= end):
                continue
            schedule_cache[team].append(
                {
                    "game_id": view.game_id,
                    "matchup": f"{view.team_abbrev} vs {view.opponent_abbrev}",
                    "local_date": local_day.isoformat(),
                    "status": view.game_status_text,
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
    all_games = _schedule_games(target_date)
    games = _games_for_team_on_date(all_games, args.team, target_date, tz)
    if not games:
        print(
            f"NO: {args.team} is not playing on {target_date.isoformat()} ({args.tz})."
        )
        return 0

    game = _select_live_or_first(games)
    print(
        f"YES: {game.team_display} is playing on {target_date.isoformat()} ({args.tz})."
    )
    print(f"MATCHUP: {game.team_abbrev} vs {game.opponent_abbrev}")
    print(f"STATUS: {game.game_status_text}")
    return 0


def cmd_game_status(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.tz)
    target_date = _resolve_target_date(args.date, tz)
    all_games = _schedule_games(target_date)
    games = _games_for_team_on_date(all_games, args.team, target_date, tz)
    if not games:
        print(
            f"No game found for {args.team} on {target_date.isoformat()} ({args.tz})."
        )
        return 1

    view = _select_live_or_first(games)
    live_map = _live_game_map(target_date)
    enriched, period, clock, detail = _enrich_with_live(view, live_map)

    margin = enriched.score - enriched.opponent_score
    state = "tied"
    if margin > 0:
        state = "winning"
    elif margin < 0:
        state = "losing"

    print(f"TEAM: {enriched.team_display} ({enriched.team_abbrev})")
    print(f"OPPONENT: {enriched.opponent_display} ({enriched.opponent_abbrev})")
    print(
        f"SCORE: {enriched.team_abbrev} {enriched.score} - "
        f"{enriched.opponent_abbrev} {enriched.opponent_score}"
    )
    print(f"GAME_STATE: {state}")
    print(f"MARGIN: {margin}")
    print(f"STATUS: {detail} (Q{period}, {clock})")
    if enriched.game_id:
        print(f"GAME_ID: {enriched.game_id}")
    return 0


def _evaluate_alert(
    view: TeamView, period: int, clock: str, threshold: int, minutes_left: int
) -> tuple[bool, str]:
    in_target_period = period == 4
    secs_left = _clock_seconds(clock)
    in_window = 0 < secs_left <= minutes_left * 60
    delta = abs(view.score - view.opponent_score)
    close = delta <= threshold
    should_alert = in_target_period and in_window and close
    message = (
        f"{view.team_display} vs {view.opponent_display} | "
        f"score {view.team_abbrev} {view.score}-{view.opponent_score} {view.opponent_abbrev} | "
        f"delta={delta} | Q{period} {clock} | {view.game_status_text}"
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
        print(
            "No team specified and no followed teams in state. Use set-teams or --team."
        )
        return 1

    tz = ZoneInfo(args.tz)
    target_date = _resolve_target_date(args.date, tz)
    all_games = _schedule_games(target_date)
    live_map = _live_game_map(target_date)
    alerts = state.setdefault("alerts", {})
    emitted = 0

    for team in teams:
        games = _games_for_team_on_date(all_games, team, target_date, tz)
        if not games:
            print(f"INFO: {team} has no game on {target_date.isoformat()} ({args.tz}).")
            continue

        view = _select_live_or_first(games)
        enriched, period, clock, _ = _enrich_with_live(view, live_map)
        should_alert, detail = _evaluate_alert(
            enriched,
            period=period,
            clock=clock,
            threshold=args.threshold,
            minutes_left=args.minutes_left,
        )

        game_id = (
            enriched.game_id
            or f"{target_date.isoformat()}:{enriched.team_abbrev}:{enriched.opponent_abbrev}"
        )
        prior = alerts.get(game_id, {})
        outcome = "close" if should_alert else "not_close"
        if prior.get("outcome") == outcome:
            print(f"INFO: No state change for {team} ({outcome}).")
            continue

        alerts[game_id] = {
            "team": enriched.team_abbrev,
            "opponent": enriched.opponent_abbrev,
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
    games = _remaining_games_for_team(_schedule_games(), args.team, now_utc=now_utc)
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


def _format_close_alert(view: TeamView, period: int, clock: str) -> str:
    team_name = view.team_display.split()[-1] if view.team_display else view.team_abbrev
    opponent_name = (
        view.opponent_display.split()[-1]
        if view.opponent_display
        else view.opponent_abbrev
    )
    lines = [
        "Close Game Alert\n"
        f"{team_name} vs {opponent_name}\n"
        f"{view.team_abbrev} {view.score} - {view.opponent_score} {view.opponent_abbrev}\n"
        f"Q{period} {clock}"
    ]
    youtube_tv_url = _youtube_tv_url()
    if youtube_tv_url and view.preferred_broadcaster:
        lines.append(f"Watch: {view.preferred_broadcaster} on YouTube TV")
        lines.append(youtube_tv_url)
    elif youtube_tv_url:
        lines.append(f"Watch: {youtube_tv_url}")
    elif view.preferred_broadcaster:
        lines.append(f"Watch: {view.preferred_broadcaster} on YouTube TV")
        lines.append("https://tv.youtube.com/live")
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
            "or populate close-game-alert/data/config.json."
        )
        return 1

    print(
        f"Watching {game.team_abbrev} vs {game.opponent_abbrev} "
        f"starting {game.game_datetime_utc.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}"
    )

    game_date_arg = game.game_datetime_utc.astimezone(tz).strftime("%Y%m%d")
    while True:
        now_utc = datetime.now(UTC)
        lead_seconds = int((game.game_datetime_utc - now_utc).total_seconds())
        if lead_seconds > quiet_prestart_seconds:
            sleep_for = max(min(lead_seconds - quiet_prestart_seconds, 1800), 60)
            print(f"INFO: Sleeping {sleep_for}s until pregame window.")
            await asyncio.sleep(sleep_for)
            continue

        game_day = _resolve_target_date(game_date_arg, tz)
        all_games = _schedule_games(game_day)
        games = _games_for_team_on_date(all_games, team, game_day, tz)
        if not games:
            print(f"INFO: No scheduled game found for {team} on {game_date_arg}.")
            return 1

        view = _select_live_or_first(games)
        live_map = _live_game_map(game_day)
        enriched, period, clock, detail = _enrich_with_live(view, live_map)

        if enriched.game_status >= 3 or "final" in detail.lower():
            alerts[enriched.game_id or game.game_id] = {
                "team": enriched.team_abbrev,
                "opponent": enriched.opponent_abbrev,
                "outcome": "final",
                "updated_at_utc": datetime.now(UTC).isoformat(),
            }
            _save_state(state)
            print(f"FINAL: {detail}")
            return 0

        if enriched.game_status < 2 and lead_seconds > 0:
            sleep_for = max(min(lead_seconds, poll_seconds), 30)
            print(
                f"INFO: Game not live yet ({detail.strip() or 'pregame'}). Sleeping {sleep_for}s."
            )
            await asyncio.sleep(sleep_for)
            continue

        should_alert, detail = _evaluate_alert(
            enriched,
            period=period,
            clock=clock,
            threshold=threshold,
            minutes_left=minutes_left,
        )
        game_key = enriched.game_id or game.game_id
        prior = alerts.get(game_key, {})
        outcome = "close" if should_alert else "not_close"

        if should_alert and prior.get("outcome") != "close":
            message = _format_close_alert(enriched, period, clock)
            if dry_run:
                print(f"ALERT: {message}")
            else:
                await _send_telegram(token, chat_id, message)
                print(f"ALERT_SENT: {message}")

        alerts[game_key] = {
            "team": enriched.team_abbrev,
            "opponent": enriched.opponent_abbrev,
            "outcome": outcome,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        }
        _save_state(state)
        print(f"INFO: {detail}")
        await asyncio.sleep(max(poll_seconds, 30))


def cmd_watch_game(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.tz)
    all_games = _schedule_games()
    now_utc = datetime.now(UTC)

    target_game: TeamGame | None = None
    if args.game_id:
        for game in _remaining_games_for_team(
            all_games, args.team, now_utc=now_utc - timedelta(days=2)
        ):
            if game.game_id == args.game_id:
                target_game = game
                break
    if target_game is None:
        remaining = _remaining_games_for_team(all_games, args.team, now_utc=now_utc)
        if args.date:
            wanted = _resolve_target_date(args.date, tz)
            for game in remaining:
                if _local_date(game.game_datetime_utc, tz) == wanted:
                    target_game = game
                    break
        elif remaining:
            target_game = remaining[0]

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

    while True:
        now_utc = datetime.now(UTC)
        remaining = _remaining_games_for_team(_schedule_games(), team, now_utc=now_utc)
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
            local_dt = next_game.game_datetime_utc.astimezone(tz).strftime(
                "%Y-%m-%d %H:%M %Z"
            )
            print(
                f"INFO: Next game {next_game.team_abbrev} vs {next_game.opponent_abbrev} at {local_dt}. Sleeping {sleep_for}s."
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
    parser = argparse.ArgumentParser(description="NBA close-game alert helper")
    sub = parser.add_subparsers(dest="command", required=True)

    set_teams = sub.add_parser("set-teams", help="Set followed team list")
    set_teams.add_argument(
        "--team", action="append", required=True, help="Team to follow"
    )
    set_teams.set_defaults(func=cmd_set_teams)

    refresh = sub.add_parser(
        "refresh-schedule", help="Cache upcoming schedule for teams"
    )
    refresh.add_argument("--team", help="Team to cache (default: all followed teams)")
    refresh.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to cache starting from start-date",
    )
    refresh.add_argument(
        "--start-date", help="Start date as YYYYMMDD (default: today in timezone)"
    )
    refresh.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    refresh.set_defaults(func=cmd_refresh_schedule)

    list_teams = sub.add_parser("list-teams", help="List followed teams")
    list_teams.set_defaults(func=cmd_list_teams)

    today = sub.add_parser("today", help="Check if team is playing today")
    today.add_argument(
        "--team", required=True, help="Team name, city, nickname, or abbreviation"
    )
    today.add_argument("--date", help="Date as YYYYMMDD (default: today in timezone)")
    today.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    today.set_defaults(func=cmd_today)

    status = sub.add_parser(
        "game-status", help="Show team score and winning or losing state"
    )
    status.add_argument(
        "--team", required=True, help="Team name, city, nickname, or abbreviation"
    )
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
    scan.add_argument(
        "--threshold", type=int, default=6, help="Max score delta for close game"
    )
    scan.add_argument(
        "--minutes-left", type=int, default=5, help="Minutes remaining threshold in Q4"
    )
    scan.add_argument(
        "--notify-non-close",
        action="store_true",
        help="Emit alerts for non-close outcomes too (state-change only)",
    )
    scan.set_defaults(func=cmd_alert_scan)

    upcoming = sub.add_parser("upcoming", help="List remaining scheduled games")
    upcoming.add_argument(
        "--team", required=True, help="Team name, city, nickname, or abbreviation"
    )
    upcoming.add_argument(
        "--limit", type=int, default=12, help="Number of upcoming games to show"
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
    watch.add_argument(
        "--team", required=True, help="Team name, city, nickname, or abbreviation"
    )
    watch.add_argument("--game-id", help="Specific game id to watch")
    watch.add_argument("--date", help="Local game date as YYYYMMDD")
    watch.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    watch.add_argument(
        "--threshold", type=int, default=6, help="Max score delta for close game"
    )
    watch.add_argument(
        "--minutes-left", type=int, default=5, help="Minutes remaining threshold in Q4"
    )
    watch.add_argument(
        "--poll-seconds",
        type=int,
        default=120,
        help="Scoreboard poll interval during game",
    )
    watch.add_argument(
        "--quiet-prestart-seconds",
        type=int,
        default=900,
        help="Start checking this many seconds before listed tipoff",
    )
    watch.add_argument(
        "--dry-run", action="store_true", help="Print alerts without Telegram"
    )
    watch.set_defaults(func=cmd_watch_game)

    daemon = sub.add_parser(
        "daemon",
        help="Long-running watcher that sleeps until tipoff, then polls during games only",
    )
    daemon.add_argument(
        "--team", required=True, help="Team name, city, nickname, or abbreviation"
    )
    daemon.add_argument(
        "--tz",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone (default: America/Los_Angeles)",
    )
    daemon.add_argument(
        "--threshold", type=int, default=6, help="Max score delta for close game"
    )
    daemon.add_argument(
        "--minutes-left", type=int, default=5, help="Minutes remaining threshold in Q4"
    )
    daemon.add_argument(
        "--poll-seconds",
        type=int,
        default=120,
        help="Scoreboard poll interval during games",
    )
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
    daemon.add_argument(
        "--dry-run", action="store_true", help="Print alerts without Telegram"
    )
    daemon.set_defaults(func=cmd_daemon)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"Error fetching NBA data: {exc}")
        return 2
    except ValueError as exc:
        print(f"Input error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
