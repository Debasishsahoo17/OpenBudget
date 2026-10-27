"""SQLite database connection, schema, and sample-data utilities."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "openbudget.sqlite3"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 2 AND 80),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'citizen' CHECK(role IN ('citizen', 'moderator')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    year INTEGER NOT NULL,
    description TEXT NOT NULL,
    total_budget INTEGER NOT NULL CHECK(total_budget > 0),
    stage TEXT NOT NULL CHECK(stage IN ('submissions', 'voting', 'closed', 'allocated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    district TEXT NOT NULL,
    requested_amount INTEGER NOT NULL CHECK(requested_amount > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','funded','unfunded')),
    verification_note TEXT NOT NULL DEFAULT '',
    verified_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_cycle_status ON proposals(cycle_id, status);
CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(cycle_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_votes_proposal ON votes(proposal_id);
CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    proposal_id INTEGER NOT NULL UNIQUE REFERENCES proposals(id),
    amount INTEGER NOT NULL CHECK(amount > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    cycle_id INTEGER REFERENCES cycles(id),
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_recent ON audit_events(id DESC);
"""


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback and close at the end of a with-block."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    location = str(path or os.environ.get("OPENBUDGET_DB") or DEFAULT_DB)
    if location != ":memory:":
        Path(location).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(location, timeout=15, isolation_level=None, check_same_thread=False, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    if location != ":memory:":
        db.execute("PRAGMA journal_mode=WAL")
    return db


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def record_audit(db, actor_id, cycle_id, subject_type, subject_id, action, detail=""):
    db.execute("""INSERT INTO audit_events
        (actor_id,cycle_id,subject_type,subject_id,action,detail)
        VALUES (?,?,?,?,?,?)""", (actor_id, cycle_id, subject_type, subject_id, action, detail))
