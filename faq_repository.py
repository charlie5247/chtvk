"""Persistence operations for FAQ records."""

import json
import sqlite3

from models import FAQ, utc_now


class FAQRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def upsert(self, faq: FAQ) -> int:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO faq
                (category, question, answer, keywords, aliases, priority, active,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(question) DO UPDATE SET
                category=excluded.category,
                answer=excluded.answer,
                keywords=excluded.keywords,
                aliases=excluded.aliases,
                updated_at=excluded.updated_at
            """,
            (faq.category, faq.question, faq.answer,
             json.dumps(faq.keywords, ensure_ascii=False),
             json.dumps(faq.aliases, ensure_ascii=False), faq.priority,
             int(faq.active), faq.created_at, now),
        )
        row = self.connection.execute(
            "SELECT id FROM faq WHERE question = ? COLLATE NOCASE", (faq.question,)
        ).fetchone()
        return int(row["id"])

    def all(self) -> list[dict]:
        rows = self.connection.execute("SELECT * FROM faq ORDER BY id").fetchall()
        return [
            {**dict(row), "keywords": json.loads(row["keywords"]),
             "aliases": json.loads(row["aliases"]), "active": bool(row["active"])}
            for row in rows
        ]

    def active(self) -> list[dict]:
        return [faq for faq in self.all() if faq["active"]]

    def get_active(self, faq_id: int) -> dict | None:
        return next((faq for faq in self.active() if faq["id"] == faq_id), None)

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM faq").fetchone()[0])
