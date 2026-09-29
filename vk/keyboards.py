"""VK-compatible keyboard dictionaries with server-validated callback payloads."""

import json


def _button(label: str, payload: dict, color: str = "secondary") -> dict:
    return {"action": {"type": "callback", "label": label[:40], "payload": json.dumps(payload, ensure_ascii=False)}, "color": color}


def operator_keyboard() -> dict:
    return {"inline": True, "buttons": [
        [_button("Все вопросы", {"action": "all_questions"})],
        [_button("Связаться с оператором", {"action": "operator"}, "primary")],
    ]}


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
    buttons = [[_button(option["question"], {"action": "clarification", "faq_id": option["faq_id"], "nonce": option["nonce"]})] for option in unique]
    buttons.append([_button("Связаться с оператором", {"action": "operator"}, "primary")])
    return {"inline": True, "buttons": buttons}


def faq_menu_keyboard(options: list[dict], page: int, page_size: int = 5) -> dict:
    start = page * page_size
    visible = options[start:start + page_size]
    buttons = [[_button(item["question"], {"action": "faq_select", "faq_id": item["faq_id"], "nonce": item["nonce"]})] for item in visible]
    navigation = []
    nonce = options[0]["nonce"] if options else ""
    if page > 0:
        navigation.append(_button("Назад", {"action": "faq_page", "page": page - 1, "nonce": nonce}))
    if start + page_size < len(options):
        navigation.append(_button("Далее", {"action": "faq_page", "page": page + 1, "nonce": nonce}))
    if navigation:
        buttons.append(navigation)
    buttons.append([_button("Связаться с оператором", {"action": "operator"}, "primary")])
    return {"inline": True, "buttons": buttons}
