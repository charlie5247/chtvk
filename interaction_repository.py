"""Persistence for unknown questions and message history."""

import sqlite3
import json
import secrets
from datetime import datetime, timedelta, timezone

from models import utc_now


class InteractionRepository:
    def __init__(self, connection: sqlite3.Connection, duplicate_window_minutes: int = 10, auto_commit: bool = True):
        self.connection = connection
        self.duplicate_window_minutes = duplicate_window_minutes
        self.auto_commit = auto_commit

    def _commit(self) -> None:
        if self.auto_commit:
            self.connection.commit()

    def save_unknown(self, vk_user_id: int, text: str, normalized_text: str, best_score: float) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=self.duplicate_window_minutes)).isoformat(timespec="seconds")
        duplicate = self.connection.execute(
            "SELECT 1 FROM unknown_questions WHERE vk_user_id=? AND normalized_text=? AND created_at>=? LIMIT 1",
            (vk_user_id, normalized_text, cutoff),
        ).fetchone()
        if duplicate:
            return False
        self.connection.execute(
            "INSERT INTO unknown_questions(vk_user_id,text,normalized_text,best_score,created_at) VALUES(?,?,?,?,?)",
            (vk_user_id, text, normalized_text, best_score, utc_now()),
        )
        self._commit()
        return True

    def save_message(self, vk_user_id: int, direction: str, text: str, response_type: str | None = None, faq_id: int | None = None, confidence: str | None = None) -> None:
        if direction not in {"incoming", "outgoing"}:
            raise ValueError("Некорректное направление сообщения")
        self.connection.execute(
            "INSERT INTO messages(vk_user_id,direction,text,response_type,faq_id,confidence,created_at) VALUES(?,?,?,?,?,?,?)",
            (vk_user_id, direction, text, response_type, faq_id, confidence, utc_now()),
        )
        self._commit()

    def save_clarification(self, vk_user_id: int, faq_ids: list[int], nonce: str, ttl_minutes: int = 10) -> None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=ttl_minutes)
        self.connection.execute(
            "INSERT INTO pending_clarifications(vk_user_id,faq_ids,nonce,created_at,expires_at) VALUES(?,?,?,?,?) ON CONFLICT(vk_user_id) DO UPDATE SET faq_ids=excluded.faq_ids,nonce=excluded.nonce,created_at=excluded.created_at,expires_at=excluded.expires_at",
            (vk_user_id, json.dumps(faq_ids), nonce, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
        self._commit()

    def consume_clarification(self, vk_user_id: int, faq_id: int, nonce: str) -> bool:
        row = self.connection.execute("SELECT faq_ids,nonce,expires_at FROM pending_clarifications WHERE vk_user_id=?", (vk_user_id,)).fetchone()
        now = utc_now()
        valid = bool(row and row["expires_at"] >= now and secrets.compare_digest(row["nonce"], nonce) and faq_id in json.loads(row["faq_ids"]))
        self.connection.execute("DELETE FROM pending_clarifications WHERE vk_user_id=?", (vk_user_id,))
        self._commit()
        return valid

    def clear_clarification(self, vk_user_id: int) -> None:
        self.connection.execute("DELETE FROM pending_clarifications WHERE vk_user_id=?", (vk_user_id,))
        self._commit()

    def delete_user_data(self, vk_user_id: int) -> None:
        self.connection.execute("DELETE FROM messages WHERE vk_user_id=?", (vk_user_id,))
        self.connection.execute("DELETE FROM unknown_questions WHERE vk_user_id=?", (vk_user_id,))
        self.connection.execute("DELETE FROM pending_clarifications WHERE vk_user_id=?", (vk_user_id,))
        self.connection.execute("DELETE FROM users WHERE vk_user_id=?", (vk_user_id,))
        self._commit()

    def purge_messages_before(self, cutoff: str) -> int:
        cursor = self.connection.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        self._commit()
        return cursor.rowcount

    def purge_unknown_before(self, cutoff: str) -> int:
        cursor = self.connection.execute("DELETE FROM unknown_questions WHERE created_at < ?", (cutoff,))
        self._commit()
        return cursor.rowcount

    def purge_expired_clarifications(self, now: str | None = None) -> int:
        cursor = self.connection.execute("DELETE FROM pending_clarifications WHERE expires_at < ?", (now or utc_now(),))
        self._commit()
        return cursor.rowcount
