"""SQLite storage. One connection per unit of work; WAL lets the pages read while a publish writes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Every accepted document as received (body), and the validated copy the pages read (doc).
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    body TEXT NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_library ON documents (library, id);
CREATE UNIQUE INDEX IF NOT EXISTS documents_sha ON documents (library, sha256);
-- Publishes turned away; never their bodies.
CREATE TABLE IF NOT EXISTS rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    library TEXT NOT NULL,
    status INTEGER NOT NULL,
    reason TEXT NOT NULL,
    size INTEGER NOT NULL
);
-- One row per library and run; a run published twice keeps its latest document.
CREATE TABLE IF NOT EXISTS runs (
    library TEXT NOT NULL,
    run_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    documents INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL,
    mode TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    next_run_at TEXT NOT NULL,
    cap INTEGER NOT NULL,
    blocks TEXT NOT NULL,
    going_n INTEGER NOT NULL,
    going_gb REAL NOT NULL,
    later_n INTEGER NOT NULL,
    kept_n INTEGER NOT NULL,
    needs_n INTEGER NOT NULL,
    deleted_n INTEGER NOT NULL,
    deleted_gb REAL NOT NULL,
    summary TEXT NOT NULL,
    digest TEXT NOT NULL DEFAULT '',
    judge_status TEXT,
    judge_batches INTEGER,
    judge_failed INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    model TEXT,
    PRIMARY KEY (library, run_key)
);
-- The latest facts a document gave for each title.
CREATE TABLE IF NOT EXISTS titles (
    library TEXT NOT NULL,
    id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    size_gb REAL,
    imdb_score REAL,
    imdb_votes INTEGER,
    plays INTEGER,
    finished_by INTEGER,
    origin TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    overview TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    PRIMARY KEY (library, id)
);
-- Every distinct verdict a title has had; a verdict repeated run after run is one row.
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    section TEXT NOT NULL,
    verdict TEXT NOT NULL,
    by TEXT NOT NULL,
    reason TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    simulated INTEGER NOT NULL,
    first_run TEXT NOT NULL,
    last_run TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS verdicts_title ON verdicts (library, item_id, id);
-- What the engines did: deleted, or would have deleted in a dry run.
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    size_gb REAL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    run_key TEXT NOT NULL,
    UNIQUE (library, item_id, kind, run_key)
);
-- Each button press that changed a tag. page: week (/) or allow (/allow); added: 1 add, 0 remove.
CREATE TABLE IF NOT EXISTS taps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    page TEXT NOT NULL,
    action TEXT NOT NULL,
    tag TEXT NOT NULL,
    added INTEGER NOT NULL,
    document_id INTEGER,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    undone_at TEXT,
    undo_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS taps_title ON taps (library, item_id, id);
-- The libraries as the services last returned them: the allow list is read from here.
CREATE TABLE IF NOT EXISTS catalogue (
    library TEXT NOT NULL,
    id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    size_gb REAL,
    imdb_score REAL,
    imdb_votes INTEGER,
    genres TEXT NOT NULL,
    tags TEXT NOT NULL,
    slug TEXT NOT NULL,
    PRIMARY KEY (library, id)
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        with self.tx() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self.tx() as c:
            return c.execute(sql, args).fetchone()

    def count(self, sql: str, *args: Any) -> int:
        """The first column of the first row, as a whole number: for `SELECT count(*)` queries."""
        row = self.one(sql, *args)
        return int(row[0]) if row and row[0] is not None else 0

    def run(self, sql: str, *args: Any) -> int:
        with self.tx() as c:
            cur = c.execute(sql, args)
            return int(cur.lastrowid or 0)

    def get(self, key: str, default: Any) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self.run(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key,
            json.dumps(value),
        )

    def latest(self, library: str) -> tuple[int, dict] | None:
        """The newest accepted document for a library, as (id, validated copy)."""
        row = self.one(
            "SELECT id, doc FROM documents WHERE library = ? "
            "ORDER BY generated_at DESC, id DESC LIMIT 1",
            library,
        )
        return (row["id"], json.loads(row["doc"])) if row else None
