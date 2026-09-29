import json
from datetime import datetime, timedelta, timezone

import pytest

from database import connect
from faq_search import FAQSearch
from import_docx import import_faq
from models import FAQ, utc_now
from faq_repository import FAQRepository
from vk.adapter import AdapterStatus, STALE_MENU_TEXT, VKAdapter
from vk.client import MockVKClient
from vk.config import VKConfig

from test_vk_adapter import callback, message


@pytest.fixture()
def menu_env(tmp_path):
    db = tmp_path / "menu.db"
    import_faq("chat_bot.docx", db, tmp_path / "export", tmp_path / "report")
    client = MockVKClient()
    adapter = VKAdapter(lambda: connect(db), client, VKConfig(group_id=77, db_path=str(db), rate_limit_per_minute=100), sleep=lambda _: None)
    return db, client, adapter


def payload(button):
    return json.loads(button["action"]["payload"])


def open_menu(client, adapter, user=100, event_id="open"):
    result = adapter.handle_event(callback(event_id, {"action": "all_questions"}, user))
    return result, client.sent[-1].keyboard


def outgoing(event_id, text, user=100, sender=-77, peer=None, out=1):
    return {"type": "message_new", "event_id": event_id, "object": {"message": {
        "from_id": sender, "peer_id": peer or user, "text": text, "out": out,
        "conversation_message_id": 2, "date": 1,
    }}}


def test_menu_lists_active_deterministically_and_paginates(menu_env):
    db, client, adapter = menu_env
    connection = connect(db)
    rows = connection.execute("SELECT id FROM faq ORDER BY priority,id").fetchall()
    inactive = rows[0][0]
    connection.execute("UPDATE faq SET active=0 WHERE id=?", (inactive,))
    connection.commit(); connection.close()
    result, keyboard = open_menu(client, adapter)
    assert result.response_type == "FAQ_MENU"
    assert client.sent[-1].text == "Выберите интересующий вопрос:"
    assert client.callbacks[-1]["text"] == "Список вопросов открыт"
    first_ids = [payload(row[0])["faq_id"] for row in keyboard["buttons"][:5]]
    expected = [row[0] for row in connect(db).execute("SELECT id FROM faq WHERE active=1 ORDER BY priority,id LIMIT 5")]
    assert first_ids == expected and inactive not in first_ids
    assert len(keyboard["buttons"]) <= 7 and all(len(row) <= 2 for row in keyboard["buttons"])
    next_payload = payload(keyboard["buttons"][-2][-1])
    assert next_payload["action"] == "faq_page"
    adapter.handle_event(callback("next", next_payload))
    assert client.callbacks[-1]["text"] == "Страница открыта"
    assert payload(client.sent[-1].keyboard["buttons"][-2][0])["action"] == "faq_page"


def test_long_labels_are_visual_only_and_within_vk_limit(menu_env):
    db, client, adapter = menu_env
    connection = connect(db)
    long_question = "Очень длинный вопрос " * 10
    FAQRepository(connection).upsert(FAQ("x", long_question, "answer", [], [], 0, True, utc_now(), utc_now()))
    connection.commit(); connection.close()
    _, keyboard = open_menu(client, adapter)
    label = keyboard["buttons"][0][0]["action"]["label"]
    assert len(label) <= 40
    assert connect(db).execute("SELECT question FROM faq WHERE priority=0").fetchone()[0] == long_question


def test_select_reads_database_without_search_and_consumes_state(menu_env, monkeypatch):
    db, client, adapter = menu_env
    _, keyboard = open_menu(client, adapter)
    selected = payload(keyboard["buttons"][0][0])
    connection = connect(db); connection.execute("UPDATE faq SET answer='fresh database answer' WHERE id=?", (selected["faq_id"],)); connection.commit(); connection.close()
    monkeypatch.setattr(FAQSearch, "search", lambda *_: pytest.fail("FAQSearch must not run"))
    result = adapter.handle_event(callback("select", selected))
    assert result.response_type == "FAQ_ANSWER" and client.sent[-1].text == "fresh database answer"
    assert "all_questions" in client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"]
    assert adapter.handle_event(callback("reuse", selected)).status is AdapterStatus.INVALID_EVENT
    assert client.callbacks[-1]["text"] == STALE_MENU_TEXT


@pytest.mark.parametrize("change", [
    lambda p: p.pop("nonce"),
    lambda p: p.update(nonce="wrong"),
    lambda p: p.update(faq_id=True),
    lambda p: p.update(faq_id=0),
    lambda p: p.update(faq_id=999999),
])
def test_forged_menu_selection_is_rejected(menu_env, change):
    _, client, adapter = menu_env
    _, keyboard = open_menu(client, adapter)
    selected = payload(keyboard["buttons"][0][0]); change(selected)
    assert adapter.handle_event(callback("bad-" + str(len(client.callbacks)), selected)).status is AdapterStatus.INVALID_EVENT
    assert client.callbacks[-1]["text"] == STALE_MENU_TEXT


def test_expired_cross_user_inactive_and_invalid_page_rejected(menu_env):
    db, client, adapter = menu_env
    _, keyboard = open_menu(client, adapter, 100)
    selected = payload(keyboard["buttons"][0][0])
    assert adapter.handle_event(callback("cross", selected, 101)).status is AdapterStatus.INVALID_EVENT
    page = {"action": "faq_page", "page": 999, "nonce": selected["nonce"]}
    assert adapter.handle_event(callback("page", page)).status is AdapterStatus.INVALID_EVENT
    connection = connect(db); connection.execute("UPDATE faq SET active=0 WHERE id=?", (selected["faq_id"],)); connection.commit(); connection.close()
    assert adapter.handle_event(callback("inactive", selected)).status is AdapterStatus.INVALID_EVENT
    _, keyboard = open_menu(client, adapter, 102, "open-exp")
    selected = payload(keyboard["buttons"][0][0])
    connection = connect(db); connection.execute("UPDATE pending_faq_menus SET expires_at=? WHERE vk_user_id=102", ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(timespec="seconds"),)); connection.commit(); connection.close()
    assert adapter.handle_event(callback("expired", selected, 102)).status is AdapterStatus.INVALID_EVENT


def test_menu_dedup_and_operator_handoff_invalidates_old_state(menu_env):
    _, client, adapter = menu_env
    event = callback("same-open", {"action": "all_questions"})
    assert adapter.handle_event(event).response_type == "FAQ_MENU"
    assert adapter.handle_event(event).status is AdapterStatus.DUPLICATE
    selected = payload(client.sent[-1].keyboard["buttons"][0][0])
    next_page = payload(client.sent[-1].keyboard["buttons"][-2][-1])
    page_event = callback("same-page", next_page)
    assert adapter.handle_event(page_event).response_type == "FAQ_MENU"
    assert adapter.handle_event(page_event).status is AdapterStatus.DUPLICATE
    select_event = callback("same-select", selected)
    sends = client.call_count
    assert adapter.handle_event(select_event).response_type == "FAQ_ANSWER"
    assert adapter.handle_event(select_event).status is AdapterStatus.DUPLICATE
    assert client.call_count == sends + 1
    open_menu(client, adapter, event_id="reopen")
    selected = payload(client.sent[-1].keyboard["buttons"][0][0])
    adapter.handle_event(callback("operator", {"action": "operator"}))
    assert adapter.handle_event(callback("old", selected)).status is AdapterStatus.NO_AUTOREPLY


@pytest.mark.parametrize("command", ["/бот", "/bot", "  /БОТ  "])
def test_outgoing_admin_returns_operator_to_bot(menu_env, command):
    db, client, adapter = menu_env
    adapter.handle_event(callback("op-" + command, {"action": "operator"}))
    before = client.call_count
    event = outgoing("admin-" + command, command)
    assert adapter.handle_event(event).response_type == "SWITCHED_TO_BOT"
    assert client.call_count == before + 1 and client.sent[-1].text.startswith("Автоматический помощник")
    assert adapter.handle_event(event).status is AdapterStatus.DUPLICATE
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "bot"


@pytest.mark.parametrize("text", ["бот", "/бот сейчас", "пожалуйста /бот", "верни /бот", "обычный ручной ответ"])
def test_ordinary_outgoing_text_does_not_transition(menu_env, text):
    db, client, adapter = menu_env
    adapter.handle_event(callback("op-" + str(len(text)), {"action": "operator"}))
    sends = client.call_count
    assert adapter.handle_event(outgoing("out-" + str(len(text)), text)).status is AdapterStatus.NO_AUTOREPLY
    assert client.call_count == sends
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "operator"


def test_admin_security_and_pending_cleanup(menu_env):
    db, client, adapter = menu_env
    adapter.handle_event(message("clarify", "для чего нужен электронный"))
    open_menu(client, adapter, event_id="menu")
    adapter.handle_event(callback("op", {"action": "operator"}))
    for forged in (
        outgoing("wrong-sender", "/бот", sender=-88),
        outgoing("group", "/бот", peer=2_000_000_001),
    ):
        adapter.handle_event(forged)
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "operator"
    # Incoming /бот follows the existing user command normalization, rather
    # than the outgoing admin path, and is recorded as incoming.
    adapter.handle_event(message("incoming", "/бот"))
    connection = connect(db)
    assert connection.execute("SELECT COUNT(*) FROM messages WHERE vk_user_id=100 AND direction='incoming' AND text='/бот'").fetchone()[0] == 1
    adapter.handle_event(callback("op-again", {"action": "operator"}))
    connection.execute("INSERT INTO pending_faq_menus(vk_user_id,faq_ids,nonce,created_at,expires_at) VALUES(?,?,?,?,?)", (100, "[1]", "stale", utc_now(), "2999-01-01T00:00:00+00:00"))
    connection.execute("INSERT OR REPLACE INTO pending_clarifications(vk_user_id,faq_ids,nonce,created_at,expires_at) VALUES(?,?,?,?,?)", (100, "[1]", "stale", utc_now(), "2999-01-01T00:00:00+00:00"))
    connection.commit(); connection.close()
    adapter.handle_event(outgoing("valid", "/бот"))
    connection = connect(db)
    assert connection.execute("SELECT COUNT(*) FROM pending_clarifications WHERE vk_user_id=100").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM pending_faq_menus WHERE vk_user_id=100").fetchone()[0] == 0
    assert connection.execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "bot"


def test_existing_user_return_ux_is_preserved(menu_env):
    _, client, adapter = menu_env
    adapter.handle_event(callback("operator", {"action": "operator"}))
    result = adapter.handle_event(callback("bot", {"action": "bot"}))
    assert result.response_type == "SWITCHED_TO_BOT"
    assert client.sent[-1].text == "Автоматический помощник снова включён. Можете задать вопрос."
    assert client.callbacks[-1]["text"] == "Автоматический помощник включён"
