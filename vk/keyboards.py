"""VK-compatible keyboard dictionaries with server-validated callback payloads."""

import json


def _button(label: str, payload: dict, color: str = "secondary") -> dict:
    return {"action": {"type": "callback", "label": label[:40], "payload": json.dumps(payload, ensure_ascii=False)}, "color": color}


def operator_keyboard() -> dict:
    return {"inline": True, "buttons": [[_button("Связаться с оператором", {"action": "operator"}, "primary")]]}


def bot_keyboard() -> dict:
    return {"inline": True, "buttons": [[_button("Вернуться к боту", {"action": "bot"}, "primary")]]}


def clarification_keyboard(options: list[dict]) -> dict:
    unique = []
    seen: set[int] = set()
    for option in options:
        if option["faq_id"] not in seen:
            seen.add(option["faq_id"])
            unique.append(option)
        if len(unique) == 3:
            break
    return {"inline": True, "buttons": [[_button(option["question"], {"action": "clarification", "faq_id": option["faq_id"], "nonce": option["nonce"]})] for option in unique]}
