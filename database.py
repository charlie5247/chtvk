"""SQLite connection and schema management."""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS faq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    question TEXT NOT NULL COLLATE NOCASE UNIQUE,
    answer TEXT NOT NULL,
    keywords TEXT NOT NULL,
    aliases TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    vk_user_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'bot' CHECK (mode IN ('bot', 'operator')),
    current_category TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unknown_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vk_user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    best_score REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unknown_user_text_time
    ON unknown_questions(vk_user_id, normalized_text, created_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vk_user_id INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    text TEXT NOT NULL,
    response_type TEXT,
    faq_id INTEGER,
    confidence TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (faq_id) REFERENCES faq(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_user_time
    ON messages(vk_user_id, created_at);
CREATE TABLE IF NOT EXISTS pending_clarifications (
    vk_user_id INTEGER PRIMARY KEY,
    faq_ids TEXT NOT NULL,
    nonce TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (vk_user_id) REFERENCES users(vk_user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS processed_vk_events (
    event_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('PROCESSING', 'DONE', 'FAILED')),
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_vk_events_expiry
    ON processed_vk_events(expires_at);
"""


def connect(path: str | Path = "data/bot.db") -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(SCHEMA)
    connection.executescript(USER_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(pending_clarifications)")}
    if "nonce" not in columns:
        connection.execute("ALTER TABLE pending_clarifications ADD COLUMN nonce TEXT NOT NULL DEFAULT ''")
    logger.info("SQLite database loaded")
    return connection
