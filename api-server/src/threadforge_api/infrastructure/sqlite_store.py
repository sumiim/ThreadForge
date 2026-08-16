"""SQLite control-plane store.

A single-process, thread-safe SQLite backend for the control-plane records that
the JSON repositories previously owned (tasks, approvals) plus the new P1
control-plane tables: runs, workspace leases, event cursors and normalized
events. WAL mode + explicit ``BEGIN IMMEDIATE`` write transactions keep
concurrent reads/writes safe without a connection pool, and every mutation is
idempotent (``INSERT ... ON CONFLICT``).

The store never performs network I/O and never needs Alembic: the schema is
applied idempotently on first open and the ``meta`` table records a monotonic
schema version so future upgrades can be gated.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id               TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    workspace_id          TEXT NOT NULL,
    owner_id              TEXT NOT NULL,
    run_id                TEXT NOT NULL,
    status                TEXT NOT NULL,
    execution_environment TEXT NOT NULL DEFAULT 'backend_process',
    device_id             TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    generation            INTEGER NOT NULL DEFAULT 0,
    payload               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_session   ON tasks(session_id, owner_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run       ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    owner_id     TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    decided_at   TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id, status);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    owner_id   TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_task      ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_workspace ON runs(workspace_id);

CREATE TABLE IF NOT EXISTS workspace_leases (
    workspace_id   TEXT PRIMARY KEY,
    holder_task_id TEXT NOT NULL,
    holder_run_id  TEXT NOT NULL,
    owner_id       TEXT NOT NULL,
    lease_token    TEXT NOT NULL,
    mode           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_holder  ON workspace_leases(holder_task_id);
CREATE INDEX IF NOT EXISTS idx_leases_expires ON workspace_leases(expires_at);

CREATE TABLE IF NOT EXISTS providers (
    provider_id    TEXT PRIMARY KEY,
    owner_id       TEXT NOT NULL,
    device_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    protocol       TEXT NOT NULL,
    base_url       TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    models         TEXT NOT NULL DEFAULT '[]',
    reasoning_tier TEXT NOT NULL DEFAULT 'none',
    timeout        INTEGER NOT NULL DEFAULT 45,
    concurrency    INTEGER NOT NULL DEFAULT 1,
    state          TEXT NOT NULL DEFAULT 'active',
    is_default     INTEGER NOT NULL DEFAULT 0,
    last_test_at   TEXT NOT NULL DEFAULT '',
    last_error     TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_providers_owner ON providers(owner_id, device_id);

CREATE TABLE IF NOT EXISTS event_cursors (
    run_id        TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    sequence         INTEGER NOT NULL,
    task_id          TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    type             TEXT NOT NULL,
    phase            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '',
    summary          TEXT NOT NULL DEFAULT '',
    trace_id         TEXT NOT NULL DEFAULT '',
    parent_event_id  TEXT NOT NULL DEFAULT '',
    attempt          INTEGER,
    started_at       TEXT NOT NULL DEFAULT '',
    ended_at         TEXT NOT NULL DEFAULT '',
    timestamp        TEXT NOT NULL,
    attributes       TEXT NOT NULL DEFAULT '{}',
    payload          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq  ON events(run_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, sequence);
"""


class SqliteStore:
    """Owns one SQLite connection plus a process-local write lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.path.parent.chmod(0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; transactions are explicit
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._ensure_meta_version()

    def _ensure_meta_version(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLite schema version {row['value']} is newer than this build "
                f"({SCHEMA_VERSION}); refusing to open a downgraded database"
            )

    @property
    def root(self) -> Path:
        return self.path.parent

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True):
        """Serialize writes on the process lock and open a transaction.

        ``BEGIN IMMEDIATE`` takes the write lock up front so a concurrent
        writer cannot deadlock; the process-local RLock additionally protects
        against interleaving within one process.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def insert_event(self, event: dict) -> bool:
        """Idempotently persist a normalized public event.

        Returns True if the row was inserted, False if an identical ``event_id``
        already existed (idempotent replay).
        """
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO events(
                        event_id, sequence, task_id, run_id, type, phase, status,
                        summary, trace_id, parent_event_id, attempt, started_at,
                        ended_at, timestamp, attributes, payload
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event.get("event_id", ""),
                        int(event.get("sequence", 0)),
                        event.get("task_id", ""),
                        event.get("run_id", ""),
                        event.get("type", ""),
                        event.get("phase", ""),
                        event.get("status", ""),
                        event.get("summary", ""),
                        event.get("trace_id", ""),
                        event.get("parent_event_id", ""),
                        event.get("attempt"),
                        event.get("started_at", ""),
                        event.get("ended_at", ""),
                        event.get("timestamp", ""),
                        json.dumps(event.get("attributes", {}), ensure_ascii=False),
                        json.dumps(event.get("data", {}), ensure_ascii=False),
                    ),
                )
                inserted = cur.rowcount > 0
                self._conn.execute(
                    """
                    INSERT INTO event_cursors(run_id, last_sequence, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        last_sequence = MAX(last_sequence, excluded.last_sequence),
                        updated_at = excluded.updated_at
                    """,
                    (
                        event.get("run_id", ""),
                        int(event.get("sequence", 0)),
                        event.get("timestamp", ""),
                    ),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return inserted

    def cursor_for(self, run_id: str) -> int:
        row = self.query_one(
            "SELECT last_sequence FROM event_cursors WHERE run_id=?", (run_id,)
        )
        return int(row["last_sequence"]) if row is not None else 0

    def events_after(self, run_id: str, sequence: int, limit: int = 1000) -> list[dict]:
        rows = self.query(
            "SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence ASC LIMIT ?",
            (run_id, int(sequence), int(limit)),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["attributes"] = json.loads(item.get("attributes") or "{}")
            item["data"] = json.loads(item.get("payload") or "{}")
            out.append(item)
        return out
