from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ash.config import AshConfig
from ash.config.models import ModelConfig
from ash.integrations.email_forward_summary import (
    CONTEXT_FOOTER,
    CONTEXT_HEADER,
    CONTEXT_HEADER_RECENT,
    EmailForwardSummaryIntegration,
)
from ash.integrations.runtime import IntegrationContext
from ash.providers.base import IncomingMessage


def _seed_db(path: Path, *, telegram_message_id: int = 42) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                sender TEXT,
                received_at TEXT,
                cleaned_body TEXT,
                structured_parse_json TEXT,
                processing_status TEXT NOT NULL,
                telegram_message_id INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO emails (
                subject, sender, received_at, cleaned_body,
                structured_parse_json, processing_status, telegram_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Professional Community Updates",
                "SF Day <m@mail3.veracross.com>",
                "Tue, 02 Jun 2026 01:02:18 +0000",
                "Welcome a new principal and several staff changes for next year.",
                json.dumps(
                    {
                        "email_type": "announcement",
                        "importance": "high",
                        "parent_action_required": False,
                        "telegram_summary": "Staffing update",
                    }
                ),
                "delivered",
                telegram_message_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _config(*, db_path: Path | None, enabled: bool = True) -> AshConfig:
    config = AshConfig(
        workspace=Path("tmp-workspace"),
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    config.email_forward_summary.enabled = enabled
    config.email_forward_summary.database_path = db_path
    return config


def _context(config: AshConfig) -> IntegrationContext:
    return IntegrationContext(
        config=config,
        components=cast(Any, SimpleNamespace()),
        mode="serve",
    )


def _message(*, text: str, reply_to: str | None) -> IncomingMessage:
    return IncomingMessage(
        id="m-1",
        chat_id="c-1",
        user_id="u-1",
        text=text,
        reply_to_message_id=reply_to,
    )


@pytest.mark.asyncio
async def test_injects_context_when_reply_matches_known_email(tmp_path) -> None:
    db = tmp_path / "school.sqlite3"
    _seed_db(db, telegram_message_id=1013)
    integration = EmailForwardSummaryIntegration()
    context = _context(_config(db_path=db))
    await integration.setup(context)

    message = _message(text="who is the new principal?", reply_to="1013")
    updated = await integration.preprocess_incoming_message(message, context)

    assert CONTEXT_HEADER in updated.text
    assert CONTEXT_FOOTER in updated.text
    assert "Professional Community Updates" in updated.text
    assert "who is the new principal?" in updated.text
    assert updated.metadata["email_forward_summary.email_id"] == 1


@pytest.mark.asyncio
async def test_noop_when_disabled(tmp_path) -> None:
    db = tmp_path / "school.sqlite3"
    _seed_db(db)
    integration = EmailForwardSummaryIntegration()
    context = _context(_config(db_path=db, enabled=False))
    await integration.setup(context)

    message = _message(text="hi", reply_to="42")
    updated = await integration.preprocess_incoming_message(message, context)

    assert updated.text == "hi"
    assert "email_forward_summary.email_id" not in updated.metadata


@pytest.mark.asyncio
async def test_injects_recent_email_when_no_reply(tmp_path) -> None:
    db = tmp_path / "school.sqlite3"
    _seed_db(db, telegram_message_id=1013)
    integration = EmailForwardSummaryIntegration()
    context = _context(_config(db_path=db))
    await integration.setup(context)

    message = _message(text="who are the new science teachers?", reply_to=None)
    updated = await integration.preprocess_incoming_message(message, context)

    assert CONTEXT_HEADER_RECENT in updated.text
    assert CONTEXT_FOOTER in updated.text
    assert "Professional Community Updates" in updated.text
    assert "who are the new science teachers?" in updated.text
    assert updated.metadata["email_forward_summary.source"] == "recent"
    assert updated.metadata["email_forward_summary.email_id"] == 1


@pytest.mark.asyncio
async def test_falls_back_to_recent_when_reply_id_not_in_db(tmp_path) -> None:
    db = tmp_path / "school.sqlite3"
    _seed_db(db, telegram_message_id=42)
    integration = EmailForwardSummaryIntegration()
    context = _context(_config(db_path=db))
    await integration.setup(context)

    message = _message(text="any updates?", reply_to="999")
    updated = await integration.preprocess_incoming_message(message, context)

    assert CONTEXT_HEADER_RECENT in updated.text
    assert "any updates?" in updated.text
    assert updated.metadata["email_forward_summary.source"] == "recent"


@pytest.mark.asyncio
async def test_disabled_when_db_path_missing() -> None:
    integration = EmailForwardSummaryIntegration()
    context = _context(_config(db_path=Path("/nonexistent/path/x.sqlite3")))
    await integration.setup(context)

    message = _message(text="x", reply_to="1")
    updated = await integration.preprocess_incoming_message(message, context)
    assert updated.text == "x"


@pytest.mark.asyncio
async def test_truncates_long_body(tmp_path) -> None:
    db = tmp_path / "school.sqlite3"
    _seed_db(db, telegram_message_id=7)
    long_body = "x" * 10_000
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE emails SET cleaned_body = ? WHERE telegram_message_id = ?",
            (long_body, 7),
        )
        conn.commit()
    finally:
        conn.close()

    config = _config(db_path=db)
    config.email_forward_summary.max_body_chars = 500
    integration = EmailForwardSummaryIntegration()
    context = _context(config)
    await integration.setup(context)

    message = _message(text="explain", reply_to="7")
    updated = await integration.preprocess_incoming_message(message, context)

    assert "\u2026" in updated.text
