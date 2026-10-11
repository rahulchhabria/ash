"""Durable, bounded work state. Spec: specs/quality.md."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ash.quality.models import VERSION


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.directory = directory
        self.db = sqlite3.connect(directory / "quality.sqlite3")
        (directory / "quality.sqlite3").chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, project TEXT NOT NULL, row_id TEXT NOT NULL,
          version TEXT NOT NULL, record TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, judgment TEXT, published INTEGER NOT NULL DEFAULT 0,
          repair TEXT NOT NULL DEFAULT 'ineligible', error TEXT,
          UNIQUE(project,row_id,version));
        CREATE TABLE IF NOT EXISTS budgets(day TEXT, kind TEXT, used INTEGER, PRIMARY KEY(day,kind));
        """)
        self.db.execute("UPDATE jobs SET repair='interrupted' WHERE repair='running'")
        self.db.commit()

    def ingest(self, project: str, record: dict[str, Any]) -> str:
        row_id = str(record["id"])
        key = hashlib.sha256(f"{project}:{row_id}:{VERSION}".encode()).hexdigest()[:24]
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(id,project,row_id,version,record) VALUES (?,?,?,?,?)",
            (key, project, row_id, VERSION, json.dumps(record)),
        )
        self.db.commit()
        return key

    def reserve(self, kind: str, limit: int) -> bool:
        day = datetime.now(UTC).date().isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO budgets VALUES (?,?,0)", (day, kind))
            return (
                self.db.execute(
                    "UPDATE budgets SET used=used+1 WHERE day=? AND kind=? AND used<?",
                    (day, kind, limit),
                ).rowcount
                == 1
            )

    def update(self, key: str, **values: Any) -> None:
        allowed = {"status", "attempts", "judgment", "published", "repair", "error"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid state fields")
        self.db.execute(
            f"UPDATE jobs SET {','.join(k + '=?' for k in values)} WHERE id=?",
            (*values.values(), key),
        )
        self.db.commit()

    def get(self, key: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Unknown case")
        return dict(row)

    def pending(self, limit: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM jobs WHERE published=0 AND version=? ORDER BY rowid LIMIT ?",
                (VERSION, limit),
            )
        ]

    def next_repair(self) -> str | None:
        row = self.db.execute(
            "SELECT id FROM jobs WHERE repair='pending' AND published=1 AND version=? ORDER BY rowid LIMIT 1",
            (VERSION,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def inspect(directory: Path) -> dict[str, Any]:
        path = directory / "quality.sqlite3"
        if not path.exists():
            return {"jobs": [], "budgets": []}
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            return {
                "jobs": [
                    dict(r)
                    for r in db.execute(
                        "SELECT status,repair,count(*) AS count FROM jobs GROUP BY status,repair"
                    )
                ],
                "recent_cases": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,row_id,status,repair,error FROM jobs ORDER BY rowid DESC LIMIT 10"
                    )
                ],
                "budgets": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM budgets ORDER BY day DESC LIMIT 10"
                    )
                ],
            }
        finally:
            db.close()

    def status(self) -> dict[str, Any]:
        return self.inspect(self.directory)
