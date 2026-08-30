---
description: Monitor the Golden State Valkyries and alert when a game is close in the final five minutes of Q4
authors:
  - rahul
rationale: User wants Valkyries-focused watch alerts and quick game status answers without manual score checking
allowed_tools:
  - bash
triggers:
  - close game alert
  - valkyries alert
  - is valkyries playing today
  - is valkyries playing tonight
  - whats the score
max_iterations: 20
---

Track the user's WNBA team and answer schedule or score questions using the helper script.
Use ESPN WNBA scoreboard data as the source of truth for schedule, live status, and broadcasts.

Use this script for every operation:

```bash
uv run /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py <command> ...
```

For runtime environments where `uv run` dependency resolution is unavailable, running the
script directly with `python3` is acceptable:

```bash
python3 /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py <command> ...
```

## Commands

1. Save teams to follow:

```bash
uv run /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py set-teams --team "Golden State Valkyries"
```

2. Memorize upcoming schedule for followed teams:

```bash
uv run /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py refresh-schedule --days 14 --tz America/Los_Angeles
```

3. Check if a team is playing today:

```bash
uv run /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py today --team "Valkyries" --tz America/Los_Angeles
```

4. Check score and winning or losing:

```bash
uv run /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py game-status --team "Valkyries" --tz America/Los_Angeles
```

5. Evaluate close-game alert condition (last 5:00 of Q4, score delta <= 6):

```bash
uv run /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py alert-scan --team "Valkyries" --threshold 6 --minutes-left 5 --tz America/Los_Angeles
```

6. Evaluate all followed teams:

```bash
uv run /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py alert-scan
```

## Automation

Do not create a global every-2-minute Ash schedule for this skill.

Preferred automation is local and schedule-aware:

1. Use the WNBA schedule feed to find the next Valkyries tipoff:

```bash
python3 /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py upcoming --team "Valkyries" --tz America/Los_Angeles
```

2. For a single game, start a watcher that sleeps until tipoff and then polls only during the game:

```bash
python3 /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py watch-game --team "Valkyries" --tz America/Los_Angeles
```

3. For continuous hands-off operation, run the local daemon:

```bash
python3 /workspace/skills/valkyries-close-game-alert/scripts/wnba_close_game_alert.py daemon --team "Valkyries" --tz America/Los_Angeles
```

The daemon should be run as a local service. It sleeps between games, starts checking shortly before tipoff, polls the live scoreboard during the game, and sends Telegram only when the close-game condition first becomes true.

## Response rules

- Treat `today` and `tonight` as the user's local date in the selected timezone (default `America/Los_Angeles`).
- For "Is X playing today/tonight?": answer yes/no first in the first sentence.
- If no game: say `No, <team> is not playing tonight (<YYYY-MM-DD local date>).`
- If yes: include matchup and start or status detail.
- Do not lead with "they played last night" when answering a yes/no schedule question.
- Use helper script output as the source of truth for schedule and score.
- For "What's the score?": include quarter or clock and both team scores.
- For "Is X winning/losing?": answer winning, losing, or tied first, then include margin.
- For alert scans: surface any `ALERT:` lines immediately.
- For scheduled alert scans: if `ALERT_COUNT: 0`, respond with exactly `[NO_REPLY]`.
- For ad hoc or manual alert scans: if no alert condition is met, explicitly say no close-game alert is active.
- When asked to "set up automatic alerts", prefer the local daemon over Ash schedule skill loops.
