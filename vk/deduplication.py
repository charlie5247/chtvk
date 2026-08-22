"""SQLite-backed concurrent event claiming and TTL maintenance."""

import sqlite3
from datetime import datetime, timedelta, timezone


class EventDeduplicator:
    def __init__(self, connection: sqlite3.Connection, ttl_seconds: int):
        self.connection = connection
        self.ttl_seconds = ttl_seconds

    def claim(self, event_id: str) -> bool:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=self.ttl_seconds)).isoformat(timespec="seconds")
        try:
            self.connection.execute(
                "INSERT INTO processed_vk_events(event_id,status,received_at,expires_at) VALUES(?, 'PROCESSING', ?, ?)",
                (event_id, now.isoformat(timespec="seconds"), expires),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            self.connection.rollback()
        # A crashed PROCESSING/FAILED claim is deliberately suppressed until its
        # TTL expires. Reclaim is serialized to remain safe across workers.
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute(
            "SELECT expires_at FROM processed_vk_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row and row["expires_at"] >= now.isoformat(timespec="seconds"):
            self.connection.rollback()
            return False
        self.connection.execute("DELETE FROM processed_vk_events WHERE event_id=?", (event_id,))
        self.connection.execute(
            "INSERT INTO processed_vk_events(event_id,status,received_at,expires_at) VALUES(?, 'PROCESSING', ?, ?)",
            (event_id, now.isoformat(timespec="seconds"), expires),
        )
        self.connection.commit()
        return True

    def finish(self, event_id: str, status: str = "DONE") -> None:
        if status not in {"DONE", "FAILED"}:
            raise ValueError("Некорректный статус события")
        self.connection.execute("UPDATE processed_vk_events SET status=? WHERE event_id=?", (status, event_id))
        self.connection.commit()

    def cleanup(self) -> int:
        cursor = self.connection.execute("DELETE FROM processed_vk_events WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
        self.connection.commit()
        return cursor.rowcount
