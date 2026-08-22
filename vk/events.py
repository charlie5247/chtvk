"""Validated internal representations of supported VK events."""

from dataclasses import dataclass
from typing import Any

from .exceptions import ValidationError


@dataclass(frozen=True, slots=True)
class IncomingVKMessage:
    event_id: str
    user_id: int
    peer_id: int
    text: str
    conversation_message_id: int | None
    timestamp: int | None
    raw_type: str
    payload: dict[str, Any] | None = None


def parse_event(raw: object) -> IncomingVKMessage | None:
    if not isinstance(raw, dict):
        raise ValidationError("VK event должен быть объектом")
    event_type = raw.get("type")
    if not isinstance(event_type, str):
        raise ValidationError("Некорректный тип VK event")
    if event_type not in {"message_new", "message_event"}:
        return None
    event_id = raw.get("event_id")
    obj = raw.get("object")
    if not isinstance(event_id, str) or not event_id.strip() or not isinstance(obj, dict):
        raise ValidationError("Отсутствует event_id или object")
    message = obj.get("message", obj) if event_type == "message_new" else obj
    if not isinstance(message, dict):
        raise ValidationError("Некорректный message")
    user_id = message.get("from_id") or message.get("user_id")
    peer_id = message.get("peer_id")
    text = message.get("text", "")
    if type(user_id) is not int or user_id <= 0 or type(peer_id) is not int or peer_id <= 0 or not isinstance(text, str):
        raise ValidationError("Некорректные поля VK message")
    cmid = message.get("conversation_message_id")
    timestamp = message.get("date")
    if cmid is not None and type(cmid) is not int:
        raise ValidationError("Некорректный conversation_message_id")
    if timestamp is not None and type(timestamp) is not int:
        raise ValidationError("Некорректный timestamp")
    payload = message.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise ValidationError("Некорректный callback payload")
    return IncomingVKMessage(event_id.strip(), user_id, peer_id, text, cmid, timestamp, event_type, payload)
