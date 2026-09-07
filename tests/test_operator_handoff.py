import json

import pytest

from database import connect
from faq_search import FAQSearch
from import_docx import import_faq
from vk.adapter import AdapterStatus, VKAdapter
from vk.client import MockVKClient
from vk.config import VKConfig

from test_vk_adapter import callback, message


@pytest.fixture()
def handoff(tmp_path):
    db = tmp_path / "handoff.db"
    import_faq("chat_bot.docx", db, tmp_path / "export.json", tmp_path / "report.json")
    client = MockVKClient()
    adapter = VKAdapter(lambda: connect(db), client, VKConfig(db_path=str(db), rate_limit_per_minute=100), sleep=lambda _: None)
    return db, client, adapter


def test_callback_handoff_is_per_user_and_bot_resumes(handoff, monkeypatch):
    db, client, adapter = handoff
    user_a, user_b = 501, 502

    assert adapter.handle_event(message("a-faq", "почему учеба по субботам", user_a)).response_type == "FAQ_ANSWER"
    operator_payload = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    assert adapter.handle_event(callback("a-operator", operator_payload, user_a)).response_type == "SWITCHED_TO_OPERATOR"
    assert client.callbacks[-1]["text"] == "Диалог передан оператору"
    assert "Пока диалог передан оператору" in client.sent[-1].text
    bot_payload = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])

    search_calls = []
    original_search = FAQSearch.search
    def recording_search(search, text):
        search_calls.append(text)
        return original_search(search, text)
    monkeypatch.setattr(FAQSearch, "search", recording_search)
    sends = client.call_count
    assert adapter.handle_event(message("a-silent", "почему учеба по субботам", user_a)).status is AdapterStatus.NO_AUTOREPLY
    assert client.call_count == sends
    assert search_calls == []
    assert adapter.handle_event(message("b-faq", "почему учеба по субботам", user_b)).response_type == "FAQ_ANSWER"
    assert search_calls == ["почему учеба по субботам"]

    assert adapter.handle_event(callback("a-bot", bot_payload, user_a)).response_type == "SWITCHED_TO_BOT"
    assert client.callbacks[-1]["text"] == "Автоматический помощник включён"
    assert client.sent[-1].text == "Автоматический помощник снова включён. Можете задать вопрос."
    assert adapter.handle_event(message("a-faq-again", "почему учеба по субботам", user_a)).response_type == "FAQ_ANSWER"

    connection = connect(db)
    assert connection.execute("SELECT mode FROM users WHERE vk_user_id=?", (user_a,)).fetchone()[0] == "bot"
    assert connection.execute("SELECT mode FROM users WHERE vk_user_id=?", (user_b,)).fetchone()[0] == "bot"
    assert connection.execute("SELECT COUNT(*) FROM messages WHERE vk_user_id=? AND direction='incoming'", (user_a,)).fetchone()[0] == 5
    connection.close()


def test_operator_transition_permanently_invalidates_pending_nonce(handoff):
    _, client, adapter = handoff
    user = 601
    assert adapter.handle_event(message("clarify", "для чего нужен электронный", user)).response_type == "CLARIFICATION"
    clarification = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    operator = json.loads(client.sent[-1].keyboard["buttons"][-1][0]["action"]["payload"])
    adapter.handle_event(callback("operator", operator, user))
    bot = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    adapter.handle_event(callback("bot", bot, user))
    assert adapter.handle_event(callback("stale", clarification, user)).response_type == "NOT_FOUND"
    assert client.callbacks[-1]["text"] == "Кнопка устарела. Попробуйте ещё раз."
