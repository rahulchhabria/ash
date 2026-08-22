#!/home/rahul/ash-triage-api/.venv/bin/python
"""Local Sentry Seer -> Codex autofix worker.

Commands:
  serve      accept Sentry webhooks and queue issue autofix jobs
  sweep      enqueue currently unresolved Sentry issues
  run-issue  run one issue immediately
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests


DEFAULT_ORG = "rc-sentry-projects"
DEFAULT_PROJECT = "school-email-service"
DEFAULT_REPO_PATH = "/home/rahul/GitHub/ash"
DEFAULT_STATE_DIR = "/home/rahul/ash-triage-api/.sentry-codex-autofix"
DEFAULT_BASE_URL = "https://sentry.io"
SEER_POLL_INTERVAL_SECONDS = 5
SEER_TIMEOUT_SECONDS = 12 * 60


class AutofixError(RuntimeError):
    """Raised when the autofix pipeline cannot continue."""


@dataclass(frozen=True)
class Settings:
    org: str
    project: str
    repo_path: Path
    state_dir: Path
    sentry_base_url: str
    sentry_auth_token: str | None
    webhook_secret: str | None
    codex_bin: str
    codex_model: str | None
    test_command: str | None
    resolve_on_success: bool


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without overwriting process env."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(args: argparse.Namespace) -> Settings:
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")
    return Settings(
        org=args.org,
        project=args.project,
        repo_path=Path(args.repo_path).expanduser().resolve(),
        state_dir=Path(args.state_dir).expanduser().resolve(),
        sentry_base_url=args.sentry_base_url.rstrip("/"),
        sentry_auth_token=os.getenv("SENTRY_AUTH_TOKEN"),
        webhook_secret=os.getenv("SENTRY_WEBHOOK_SECRET"),
        codex_bin=args.codex_bin,
        codex_model=args.codex_model,
        test_command=args.test_command,
        resolve_on_success=not args.no_resolve,
    )


class SentryClient:
    def __init__(self, settings: Settings):
        if not settings.sentry_auth_token:
            raise AutofixError("Set SENTRY_AUTH_TOKEN before contacting Sentry.")
        self._settings = settings

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._settings.sentry_base_url}{path}"
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers={
                "Authorization": f"Bearer {self._settings.sentry_auth_token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise AutofixError(
                f"Sentry {method} {path} failed with {response.status_code}: "
                f"{response.text[:500]}"
            )
        if not response.text:
            return None
        return response.json()

    def list_unresolved_issues(self, limit: int) -> list[dict[str, Any]]:
        path = f"/api/0/projects/{self._settings.org}/{self._settings.project}/issues/"
        return self._request(
            "GET",
            path,
            params={"query": "is:unresolved", "limit": limit, "sort": "date"},
        )

    def issue_detail(self, issue_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/0/organizations/{self._settings.org}/issues/{issue_id}/"
        )

    def recommended_event_markdown(self, issue_id: str) -> str:
        data = self._request(
            "GET",
            f"/api/0/organizations/{self._settings.org}/issues/"
            f"{issue_id}/events/recommended/",
            params={"llmFormat": "markdown"},
        )
        return str(data.get("formatted") or json.dumps(data, indent=2))

    def start_seer_root_cause(self, issue_id: str) -> str | None:
        data = self._request(
            "POST",
            f"/api/0/organizations/{self._settings.org}/issues/{issue_id}/autofix/",
            json_body={
                "stopping_point": "root_cause",
                "referrer": "local-codex-autofix",
            },
        )
        return data.get("sentry_run_id")

    def seer_state_markdown(self, issue_id: str) -> str:
        data = self._request(
            "GET",
            f"/api/0/organizations/{self._settings.org}/issues/{issue_id}/autofix/",
            params={"llmFormat": "markdown"},
        )
        formatted = data.get("formatted")
        if formatted:
            return str(formatted)
        return json.dumps(data, indent=2)

    def resolve_issue(self, issue_id: str) -> None:
        self._request(
            "PUT",
            f"/api/0/organizations/{self._settings.org}/issues/{issue_id}/",
            json_body={"status": "resolved"},
        )


def issue_id_from_payload(payload: dict[str, Any]) -> str | None:
    issue = payload.get("data", {}).get("issue")
    if isinstance(issue, dict) and issue.get("id"):
        return str(issue["id"])
    event = payload.get("data", {}).get("event")
    if isinstance(event, dict):
        group_id = event.get("groupID") or event.get("group_id")
        if group_id:
            return str(group_id)
    return None


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def wait_for_seer(client: SentryClient, issue_id: str, issue_dir: Path) -> str:
    deadline = time.monotonic() + SEER_TIMEOUT_SECONDS
    last_markdown = ""
    while time.monotonic() < deadline:
        markdown = client.seer_state_markdown(issue_id)
        last_markdown = markdown
        (issue_dir / "seer.md").write_text(markdown, encoding="utf-8")
        lowered = markdown.lower()
        if "root cause" in lowered and "processing" not in lowered:
            return markdown
        time.sleep(SEER_POLL_INTERVAL_SECONDS)
    if last_markdown:
        return last_markdown
    raise AutofixError(f"Timed out waiting for Seer RCA for issue {issue_id}")


def build_prompt(
    settings: Settings,
    issue_id: str,
    issue_detail: dict[str, Any],
    event_markdown: str,
    seer_markdown: str,
) -> str:
    return f"""You are running locally on the repository for a Sentry issue.

Goal:
- Fix Sentry issue {issue_id} in-place in {settings.repo_path}.
- Use the Seer root cause analysis and Sentry telemetry below as primary context.
- Keep changes tightly scoped.
- Preserve unrelated local edits.
- Add or update focused tests.
- Run the relevant tests.
- Do not create branches, commits, or pull requests.

Sentry issue:
```json
{json.dumps(issue_detail, indent=2)}
```

Recommended event:
```markdown
{event_markdown}
```

Seer root cause:
```markdown
{seer_markdown}
```
"""


def run_command(
    args: list[str],
    *,
    cwd: Path,
    stdin_text: str | None = None,
    output_path: Path | None = None,
) -> int:
    with subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        stdout, _ = process.communicate(stdin_text)
    if output_path:
        output_path.write_text(stdout or "", encoding="utf-8")
    return int(process.returncode or 0)


def run_issue(settings: Settings, issue_id: str) -> bool:
    issue_dir = settings.state_dir / issue_id
    issue_dir.mkdir(parents=True, exist_ok=True)
    lock_path = issue_dir / "RUNNING"
    done_path = issue_dir / "DONE"
    if lock_path.exists():
        print(f"issue {issue_id}: already running", file=sys.stderr)
        return False
    if done_path.exists():
        print(f"issue {issue_id}: already completed", file=sys.stderr)
        return True

    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        client = SentryClient(settings)
        detail = client.issue_detail(issue_id)
        write_json(issue_dir / "issue.json", detail)
        event_markdown = client.recommended_event_markdown(issue_id)
        (issue_dir / "event.md").write_text(event_markdown, encoding="utf-8")
        run_id = client.start_seer_root_cause(issue_id)
        if run_id:
            (issue_dir / "seer-run-id").write_text(run_id, encoding="utf-8")
        seer_markdown = wait_for_seer(client, issue_id, issue_dir)
        prompt = build_prompt(settings, issue_id, detail, event_markdown, seer_markdown)
        prompt_path = issue_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        codex_args = [
            settings.codex_bin,
            "exec",
            "-C",
            str(settings.repo_path),
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "-o",
            str(issue_dir / "codex-final.md"),
        ]
        if settings.codex_model:
            codex_args.extend(["--model", settings.codex_model])
        codex_args.append("-")
        codex_status = run_command(
            codex_args,
            cwd=settings.repo_path,
            stdin_text=prompt,
            output_path=issue_dir / "codex.log",
        )
        if codex_status != 0:
            raise AutofixError(f"Codex exited with status {codex_status}")

        if settings.test_command:
            test_status = run_command(
                ["/bin/sh", "-lc", settings.test_command],
                cwd=settings.repo_path,
                output_path=issue_dir / "tests.log",
            )
            if test_status != 0:
                raise AutofixError(f"test command exited with status {test_status}")

        if settings.resolve_on_success:
            client.resolve_issue(issue_id)
        done_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8")
        return True
    finally:
        lock_path.unlink(missing_ok=True)


def sweep(settings: Settings, limit: int) -> None:
    client = SentryClient(settings)
    issues = client.list_unresolved_issues(limit)
    for issue in issues:
        issue_id = str(issue["id"])
        title = issue.get("title", "")
        print(f"running {issue_id}: {title}")
        try:
            run_issue(settings, issue_id)
        except Exception as exc:
            issue_dir = settings.state_dir / issue_id
            issue_dir.mkdir(parents=True, exist_ok=True)
            (issue_dir / "ERROR").write_text(str(exc), encoding="utf-8")
            print(f"issue {issue_id} failed: {exc}", file=sys.stderr)


class WebhookHandler(BaseHTTPRequestHandler):
    server: "AutofixHTTPServer"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "invalid JSON")
            return

        if self.server.settings.webhook_secret:
            provided = self.headers.get("X-Autofix-Secret")
            if provided != self.server.settings.webhook_secret:
                self.send_error(403, "invalid webhook secret")
                return

        issue_id = issue_id_from_payload(payload)
        if not issue_id:
            self.send_error(202, "no issue id found")
            return

        self.server.jobs.put(issue_id)
        body = json.dumps({"queued": issue_id}).encode("utf-8")
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"webhook: {format % args}", file=sys.stderr)


class AutofixHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        settings: Settings,
        jobs: queue.Queue[str],
    ):
        super().__init__(server_address, WebhookHandler)
        self.settings = settings
        self.jobs = jobs


def worker(settings: Settings, jobs: queue.Queue[str]) -> None:
    while True:
        issue_id = jobs.get()
        try:
            run_issue(settings, issue_id)
        except Exception as exc:
            issue_dir = settings.state_dir / issue_id
            issue_dir.mkdir(parents=True, exist_ok=True)
            (issue_dir / "ERROR").write_text(str(exc), encoding="utf-8")
            print(f"issue {issue_id} failed: {exc}", file=sys.stderr)
        finally:
            jobs.task_done()


def serve(settings: Settings, host: str, port: int) -> None:
    jobs: queue.Queue[str] = queue.Queue()
    thread = threading.Thread(target=worker, args=(settings, jobs), daemon=True)
    thread.start()
    server = AutofixHTTPServer((host, port), settings, jobs)
    print(f"listening on http://{host}:{port}")
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", default=os.getenv("SENTRY_ORG", DEFAULT_ORG))
    parser.add_argument(
        "--project", default=os.getenv("SENTRY_PROJECT", DEFAULT_PROJECT)
    )
    parser.add_argument(
        "--repo-path", default=os.getenv("AUTOFIX_REPO_PATH", DEFAULT_REPO_PATH)
    )
    parser.add_argument(
        "--state-dir", default=os.getenv("AUTOFIX_STATE_DIR", DEFAULT_STATE_DIR)
    )
    parser.add_argument(
        "--sentry-base-url", default=os.getenv("SENTRY_BASE_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument("--codex-bin", default=os.getenv("CODEX_BIN", "codex"))
    parser.add_argument("--codex-model", default=os.getenv("CODEX_MODEL"))
    parser.add_argument("--test-command", default=os.getenv("AUTOFIX_TEST_COMMAND"))
    parser.add_argument("--no-resolve", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)
    run_issue_parser = subparsers.add_parser("run-issue")
    run_issue_parser.add_argument("issue_id")
    sweep_parser = subparsers.add_parser("sweep")
    sweep_parser.add_argument("--limit", type=int, default=20)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8797)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(args)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "run-issue":
        return 0 if run_issue(settings, args.issue_id) else 1
    if args.command == "sweep":
        sweep(settings, args.limit)
        return 0
    if args.command == "serve":
        serve(settings, args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
