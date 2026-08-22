"""Domain models for the FAQ knowledge base."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class FAQ:
    category: str
    question: str
    answer: str
    keywords: list[str]
    aliases: list[str]
    priority: int = 100
    active: bool = True
    id: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
