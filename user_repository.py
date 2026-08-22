"""Persistence for user mode and category state."""

import sqlite3
from dataclasses import dataclass

from models import utc_now


@dataclass(frozen=True, slots=True)
class User:
    vk_user_id: int
    mode: str
    current_category: str | None
    created_at: str
    updated_at: str


class UserRepository:
    def __init__(self, connection: sqlite3.Connection, auto_commit: bool = True):
        self.connection = connection
        self.auto_commit = auto_commit

    def _commit(self) -> None:
        if self.auto_commit:
            self.connection.commit()

    def get_or_create_user(self, vk_user_id: int) -> User:
        now = utc_now()
        self.connection.execute(
            "INSERT OR IGNORE INTO users(vk_user_id, mode, created_at, updated_at) VALUES (?, 'bot', ?, ?)",
            (vk_user_id, now, now),
        )
        self._commit()
        row = self.connection.execute("SELECT * FROM users WHERE vk_user_id = ?", (vk_user_id,)).fetchone()
        if row is None:  # pragma: no cover - defensive guard for unexpected SQLite failures
            raise sqlite3.DatabaseError("Не удалось получить или создать пользователя")
        return User(**dict(row))

    def get_user_mode(self, vk_user_id: int) -> str:
        return self.get_or_create_user(vk_user_id).mode

    def set_user_mode(self, vk_user_id: int, mode: str) -> None:
        if mode not in {"bot", "operator"}:
            raise ValueError("mode должен быть 'bot' или 'operator'")
        self.get_or_create_user(vk_user_id)
        self.connection.execute("UPDATE users SET mode = ?, updated_at = ? WHERE vk_user_id = ?", (mode, utc_now(), vk_user_id))
        self._commit()

    def set_current_category(self, vk_user_id: int, category: str | None) -> None:
        self.get_or_create_user(vk_user_id)
        self.connection.execute("UPDATE users SET current_category = ?, updated_at = ? WHERE vk_user_id = ?", (category, utc_now(), vk_user_id))
        self._commit()
