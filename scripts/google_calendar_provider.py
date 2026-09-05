#!/usr/bin/env python3
"""Tiny service-account Google Calendar capability provider for Pigeon.

Implements the bridge-v1 subprocess contract used by Ash/Pigeon capabilities.
Credentials are read from a Google service account JSON file named by
GCAL_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

VERSION = 1
CAPABILITY_ID = "gog.calendar"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_BASE_URL = "https://www.googleapis.com"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
TOKEN_CACHE_PATH = Path.home() / ".ash" / "gcal" / "token-cache.json"


class BridgeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise BridgeError("capability_invalid_input", "request must be an object")
        response = _dispatch(request)
    except BridgeError as exc:
        response = _error_response(_request_id_from_stdin_fallback(), exc.code, str(exc))
    except Exception as exc:
        response = _error_response(
            _request_id_from_stdin_fallback(),
            "capability_backend_unavailable",
            f"calendar provider failed: {exc}",
        )
    print(json.dumps(response, separators=(",", ":")))
    return 0


_LAST_REQUEST_ID = ""


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    global _LAST_REQUEST_ID
    request_id = _required_text(request.get("id"), "capability_invalid_input", "id is required")
    _LAST_REQUEST_ID = request_id
    if request.get("version") != VERSION:
        raise BridgeError("capability_invalid_input", "unsupported bridge version")
    method = _required_text(
        request.get("method"), "capability_invalid_input", "method is required"
    )
    params = request.get("params")
    if not isinstance(params, dict):
        raise BridgeError("capability_invalid_input", "params must be an object")

    if method == "definitions":
        result = _definitions()
    elif method == "auth_begin":
        result = _auth_not_required()
    elif method == "auth_complete":
        result = {"account_ref": _account_ref(params)}
    elif method == "auth_poll":
        result = {"status": "complete", "account_ref": _account_ref(params)}
    elif method == "invoke":
        result = _invoke(params)
    else:
        raise BridgeError("capability_invalid_input", f"unsupported method: {method}")
    return {"version": VERSION, "id": request_id, "result": result}


def _request_id_from_stdin_fallback() -> str:
    return _LAST_REQUEST_ID or "unknown"


def _error_response(request_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _definitions() -> dict[str, Any]:
    return {
        "definitions": [
            {
                "id": CAPABILITY_ID,
                "description": "Google Calendar operations via service account",
                "sensitive": True,
                "allowed_chat_types": ["private"],
                "operations": [
                    {
                        "name": "list_events",
                        "description": "List upcoming calendar events",
                        "requires_auth": False,
                        "mutating": False,
                    },
                    {
                        "name": "create_event",
                        "description": "Create a calendar event",
                        "requires_auth": False,
                        "mutating": True,
                    },
                ],
            }
        ]
    }


def _auth_not_required() -> dict[str, Any]:
    return {
        "auth_url": "service-account://configured-by-host",
        "flow_type": "service_account",
        "account_ref": "default",
    }


def _invoke(params: dict[str, Any]) -> dict[str, Any]:
    capability_id = _required_text(
        params.get("capability_id"), "capability_invalid_input", "capability_id is required"
    )
    if capability_id != CAPABILITY_ID:
        raise BridgeError("capability_invalid_input", f"unsupported capability: {capability_id}")
    operation = _required_text(
        params.get("operation"), "capability_invalid_input", "operation is required"
    )
    input_data = params.get("input_data")
    if not isinstance(input_data, dict):
        raise BridgeError("capability_invalid_input", "input_data must be an object")
    access_token = _access_token()
    if operation == "list_events":
        output = _list_events(input_data, access_token=access_token)
    elif operation == "create_event":
        output = _create_event(input_data, access_token=access_token)
    else:
        raise BridgeError("capability_invalid_input", f"unsupported operation: {operation}")
    return {"output": output}


def _service_account() -> dict[str, Any]:
    path = (
        _optional_text(os.environ.get("GCAL_SERVICE_ACCOUNT_FILE"))
        or _optional_text(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    )
    raw_json = _optional_text(os.environ.get("GCAL_SERVICE_ACCOUNT_JSON"))
    try:
        if raw_json:
            data = json.loads(raw_json)
        elif path:
            data = json.loads(Path(path).expanduser().read_text())
        else:
            raise BridgeError(
                "capability_auth_required",
                "set GCAL_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS",
            )
    except OSError as exc:
        raise BridgeError("capability_auth_required", f"cannot read service account: {exc}") from None
    except json.JSONDecodeError as exc:
        raise BridgeError("capability_auth_required", f"invalid service account JSON: {exc}") from None
    if not isinstance(data, dict):
        raise BridgeError("capability_auth_required", "service account JSON must be an object")
    for key in ("client_email", "private_key"):
        if not _optional_text(data.get(key)):
            raise BridgeError("capability_auth_required", f"service account missing {key}")
    return data


def _access_token() -> str:
    cached = _read_token_cache()
    now = int(time.time())
    if cached and now < int(cached.get("expires_at", 0)) - 60:
        token = _optional_text(cached.get("access_token"))
        if token:
            return token

    account = _service_account()
    assertion = _service_account_assertion(account)
    token = _http_post_form(
        TOKEN_URL,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    access_token = _optional_text(token.get("access_token"))
    if not access_token:
        error = _optional_text(token.get("error_description")) or _optional_text(token.get("error"))
        raise BridgeError(
            "capability_auth_required",
            f"Google token endpoint did not return an access token: {error or 'unknown error'}",
        )
    expires_in = _int(token.get("expires_in")) or 3600
    _write_token_cache({"access_token": access_token, "expires_at": now + expires_in})
    return access_token


def _service_account_assertion(account: dict[str, Any]) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": _required_text(account.get("client_email"), "capability_auth_required", "client_email is required"),
        "scope": CALENDAR_SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    subject = _optional_text(os.environ.get("GCAL_IMPERSONATE_SUBJECT"))
    if subject:
        claims["sub"] = subject

    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        f"{_b64url_json(header)}.{_b64url_json(claims)}".encode("ascii")
    )
    signature = _rsa_sha256_sign(
        signing_input,
        _required_text(account.get("private_key"), "capability_auth_required", "private_key is required"),
    )
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def _rsa_sha256_sign(payload: bytes, private_key: str) -> bytes:
    key_path = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False) as key_file:
            key_file.write(private_key)
            key_path = key_file.name
        os.chmod(key_path, 0o600)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path, "-binary"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        if key_path:
            try:
                os.unlink(key_path)
            except OSError:
                pass
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise BridgeError("capability_auth_required", f"openssl signing failed: {stderr}")
    return proc.stdout


def _list_events(input_data: dict[str, Any], *, access_token: str) -> dict[str, Any]:
    calendar_id = _calendar_id(input_data)
    window = _optional_text(input_data.get("window")) or "7d"
    requested_date = _optional_text(input_data.get("date"))
    tz_name = _default_timezone()
    limit = max(1, min(_int(input_data.get("limit")) or 25, 250))
    params = _list_event_params(
        window=window,
        requested_date=requested_date,
        tz_name=tz_name,
        limit=limit,
    )
    raw = _google_request(
        "GET",
        f"{_calendar_base()}/calendar/v3/calendars/{quote(calendar_id, safe='')}/events",
        access_token=access_token,
        params=params,
    )
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    events = [_event_summary(item, calendar_id) for item in items if isinstance(item, dict)]
    output = {
        "window": window,
        "events": events,
        "count": len(events),
        "calendar": calendar_id,
        "timezone": tz_name,
    }
    if requested_date:
        output["date"] = requested_date
    return output


def _list_event_params(
    *,
    window: str,
    requested_date: str | None,
    tz_name: str,
    limit: int,
) -> list[tuple[str, str]]:
    if requested_date:
        try:
            day = date.fromisoformat(requested_date)
            tz = ZoneInfo(tz_name)
        except ValueError:
            raise BridgeError("capability_invalid_input", "date must be YYYY-MM-DD") from None
        except Exception:
            raise BridgeError("capability_invalid_input", f"invalid timezone: {tz_name}") from None
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        end = start + timedelta(days=1)
        time_min = start.isoformat()
        time_max = end.isoformat()
    else:
        now = int(time.time())
        time_min = _iso8601(now)
        time_max = _iso8601(now + _parse_window_seconds(window))
    return [
        ("timeMin", time_min),
        ("timeMax", time_max),
        ("timeZone", tz_name),
        ("singleEvents", "true"),
        ("orderBy", "startTime"),
        ("maxResults", str(limit)),
    ]


def _create_event(input_data: dict[str, Any], *, access_token: str) -> dict[str, Any]:
    calendar_id = _calendar_id(input_data)
    title = _required_text(input_data.get("title"), "capability_invalid_input", "title is required")
    start = _required_text(input_data.get("start"), "capability_invalid_input", "start is required")
    end = _optional_text(input_data.get("end"))
    body: dict[str, Any] = {"summary": title, "start": _event_time(start)}
    if end:
        body["end"] = _event_time(end)
    else:
        body["end"] = _default_end(start)
    for source, target in (
        ("description", "description"),
        ("location", "location"),
        ("timezone", "timeZone"),
    ):
        value = _optional_text(input_data.get(source))
        if value and target == "timeZone":
            body["start"]["timeZone"] = value
            body["end"]["timeZone"] = value
        elif value:
            body[target] = value

    created = _google_request(
        "POST",
        f"{_calendar_base()}/calendar/v3/calendars/{quote(calendar_id, safe='')}/events",
        access_token=access_token,
        json_body=body,
    )
    event = _event_summary(created, calendar_id)
    return {"status": "created", **event}


def _calendar_id(input_data: dict[str, Any]) -> str:
    requested = _optional_text(input_data.get("calendar"))
    configured = _optional_text(os.environ.get("GCAL_DEFAULT_CALENDAR_ID"))
    if not requested or requested == "primary":
        return configured or "primary"
    return requested


def _default_timezone() -> str:
    return _optional_text(os.environ.get("GCAL_DEFAULT_TIMEZONE")) or "UTC"


def _event_summary(item: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    return {
        "id": _optional_text(item.get("id")) or "",
        "title": _optional_text(item.get("summary")) or "",
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "calendar": calendar_id,
        "location": _optional_text(item.get("location")) or "",
        "description": _optional_text(item.get("description")) or "",
        "html_link": _optional_text(item.get("htmlLink")) or "",
    }


def _event_time(value: str) -> dict[str, str]:
    if len(value) == 10:
        return {"date": value}
    return {"dateTime": value}


def _default_end(start: str) -> dict[str, str]:
    if len(start) == 10:
        try:
            return {"date": (date.fromisoformat(start) + timedelta(days=1)).isoformat()}
        except ValueError:
            return {"date": start}
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return {"dateTime": (dt + timedelta(hours=1)).isoformat()}
    except ValueError:
        return {"dateTime": start}


def _google_request(
    method: str,
    url: str,
    *,
    access_token: str,
    params: list[tuple[str, str]] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if params:
        url = f"{url}?{urlencode(params)}"
    data = None
    if json_body is not None:
        data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
    request = Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {access_token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BridgeError("capability_backend_unavailable", f"Google Calendar HTTP {exc.code}: {body[:500]}") from None
    except URLError as exc:
        raise BridgeError("capability_backend_unavailable", f"Google Calendar request failed: {exc.reason}") from None
    except json.JSONDecodeError as exc:
        raise BridgeError("capability_invalid_output", f"Google Calendar returned invalid JSON: {exc}") from None


def _http_post_form(url: str, params: dict[str, str]) -> dict[str, Any]:
    request = Request(url, data=urlencode(params).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(request, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except json.JSONDecodeError:
            raise BridgeError("capability_backend_unavailable", f"Google token HTTP {exc.code}") from None
    except URLError as exc:
        raise BridgeError("capability_backend_unavailable", f"Google token request failed: {exc.reason}") from None


def _calendar_base() -> str:
    return _optional_text(os.environ.get("GCAL_CALENDAR_API_BASE_URL")) or CALENDAR_BASE_URL


def _read_token_cache() -> dict[str, Any] | None:
    try:
        payload = json.loads(TOKEN_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_token_cache(payload: dict[str, Any]) -> None:
    try:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(json.dumps(payload, separators=(",", ":")))
        TOKEN_CACHE_PATH.chmod(0o600)
    except OSError:
        pass


def _account_ref(params: dict[str, Any]) -> str:
    return _optional_text(params.get("account_hint")) or _optional_text(params.get("account_ref")) or "default"


def _parse_window_seconds(window: str) -> int:
    if len(window) < 2:
        raise BridgeError("capability_invalid_input", "window must look like 12h, 7d, or 2w")
    try:
        value = int(window[:-1])
    except ValueError:
        raise BridgeError("capability_invalid_input", "window value must be an integer") from None
    if value <= 0:
        raise BridgeError("capability_invalid_input", "window must be positive")
    unit = window[-1].lower()
    multipliers = {"h": 3600, "d": 86400, "w": 604800}
    if unit not in multipliers:
        raise BridgeError("capability_invalid_input", "window unit must be h, d, or w")
    return value * multipliers[unit]


def _iso8601(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _b64url_json(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _required_text(value: Any, code: str, message: str) -> str:
    text = _optional_text(value)
    if not text:
        raise BridgeError(code, message)
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
