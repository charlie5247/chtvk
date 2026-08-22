"""Environment-only VK configuration without secret logging."""

import os
from dataclasses import dataclass

from .exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class VKConfig:
    mode: str = "mock"
    token: str | None = None
    group_id: int | None = None
    api_version: str = "5.199"
    rate_limit_per_minute: int = 30
    event_dedup_ttl_seconds: int = 86400
    max_input_length: int = 4096
    db_path: str = "data/bot.db"

    @classmethod
    def from_env(cls) -> "VKConfig":
        mode = os.getenv("VK_MODE", "mock").strip().lower()
        if mode not in {"mock", "production"}:
            raise ConfigurationError("VK_MODE должен быть mock или production")
        try:
            group = int(os.environ["VK_GROUP_ID"]) if os.getenv("VK_GROUP_ID") else None
            rate = int(os.getenv("VK_RATE_LIMIT_PER_MINUTE", "30"))
            ttl = int(os.getenv("VK_EVENT_DEDUP_TTL_SECONDS", "86400"))
            maximum = int(os.getenv("VK_MAX_INPUT_LENGTH", "4096"))
        except ValueError as exc:
            raise ConfigurationError("Числовая настройка VK имеет неверный формат") from exc
        config = cls(mode, os.getenv("VK_TOKEN") or None, group, os.getenv("VK_API_VERSION", "5.199"), rate, ttl, maximum, os.getenv("BOT_DB_PATH", "data/bot.db"))
        if rate <= 0 or ttl <= 0 or maximum <= 0:
            raise ConfigurationError("Лимиты VK должны быть положительными")
        if mode == "production" and (not config.token or not config.group_id):
            raise ConfigurationError("Для production необходимы VK_TOKEN и VK_GROUP_ID")
        return config
