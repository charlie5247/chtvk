import json
import threading

import pytest

from bot_logic import BotLogic, ResponseType, SWITCHED_TO_BOT_TEXT, SWITCHED_TO_OPERATOR_TEXT
from database import connect
from import_docx import import_faq
from vk.adapter import AdapterStatus, CALLBACK_REJECTED_ACK, VKAdapter
from vk.client import MockVKClient
from vk.config import VKConfig
from vk.events import parse_event
from vk.exceptions import PermanentVKError, TemporaryVKError, ValidationError
from vk.keyboards import bot_keyboard, clarification_keyboard, operator_keyboard


def message(event_id, text, user=100, peer=None):
    return {
        "type": "message_new",
        "event_id": event_id,
        "object": {"message": {
            "from_id": user, "peer_id": peer or user, "text": text,
            "conversation_message_id": 17, "date": 1_789_000_000,
        }},
    }


def callback(transport_id, action, user=100, peer=None, callback_id=None):
    return {
        "type": "message_event",
        "event_id": transport_id,
        "object": {
            "event_id": callback_id or f"vk-callback-{transport_id}",
            "user_id": user, "peer_id": peer or user,
            "conversation_message_id": 17, "payload": action,
        },
    }


@pytest.fixture()
def environment(tmp_path):
    db = tmp_path / "operator.db"
    import_faq("chat_bot.docx", db, tmp_path / "faq.json", tmp_path / "report.json")
    client = MockVKClient()
    adapter = VKAdapter(lambda: connect(db), client, VKConfig(rate_limit_per_minute=100), sleep=lambda _: None)
    return db, client, adapter


@pytest.fixture()
def populated_db(tmp_path):
    db = tmp_path / "logic.db"
    import_faq("chat_bot.docx", db, tmp_path / "faq.json", tmp_path / "report.json")
    connection = connect(db)
    yield connection
    connection.close()


def payloads(keyboard):
    return [json.loads(row[0]["action"]["payload"]) for row in keyboard["buttons"]]


def test_keyboards_are_inline_minimal_and_safe():
    for keyboard, action, label in (
        (operator_keyboard(), "operator", "Связаться с оператором"),
        (bot_keyboard(), "bot", "Вернуться к боту"),
    ):
        assert keyboard["inline"] is True
        button = keyboard["buttons"][0][0]
        assert button["action"]["type"] == "callback"
        assert button["action"]["label"] == label
        assert json.loads(button["action"]["payload"]) == {"action": action}
        assert "token" not in button["action"]["payload"].lower()


def test_clarification_keyboard_has_three_faqs_and_operator_last():
    options = [
        {"faq_id": index, "question": "Очень длинный вопрос " * 10, "nonce": "server-nonce"}
        for index in range(1, 5)
    ]
    keyboard = clarification_keyboard(options)
    assert keyboard["inline"] is True
    assert len(keyboard["buttons"]) == 4
    assert all(len(row[0]["action"]["label"]) <= 40 for row in keyboard["buttons"])
    parsed = payloads(keyboard)
    assert [item["faq_id"] for item in parsed[:3]] == [1, 2, 3]
    assert parsed[-1] == {"action": "operator"}
    assert all("answer" not in item and "token" not in json.dumps(item).lower() for item in parsed)


def test_response_keyboards_and_exact_user_texts(environment):
    _, client, adapter = environment
    cases = [
        (message("faq", "где находится институт"), "FAQ_ANSWER", "operator"),
        (message("missing", "сколько стоит обучение", 101), "NOT_FOUND", "operator"),
        (message("operator", "оператор", 102), "SWITCHED_TO_OPERATOR", "bot"),
        (message("bot", "вернуться к боту", 103), "SWITCHED_TO_BOT", "operator"),
    ]
    for event, response_type, action in cases:
        assert adapter.handle_event(event).response_type == response_type
        assert payloads(client.sent[-1].keyboard)[0] == {"action": action}
    assert client.sent[-2].text == SWITCHED_TO_OPERATOR_TEXT
    assert client.sent[-1].text == SWITCHED_TO_BOT_TEXT


def test_realistic_message_event_fields_are_distinct():
    event = parse_event(callback("transport-1", {"action": "operator"}, callback_id="callback-1"))
    assert event.event_id == "transport-1"
    assert event.callback_event_id == "callback-1"
    assert event.user_id == 100 and event.peer_id == 100
    assert event.conversation_message_id == 17


@pytest.mark.parametrize("mutator", [
    lambda raw: raw.pop("event_id"),
    lambda raw: raw["object"].pop("event_id"),
    lambda raw: raw["object"].update(user_id=0),
    lambda raw: raw["object"].update(peer_id=-1),
    lambda raw: raw["object"].update(payload="operator"),
    lambda raw: raw["object"].update(payload=[]),
])
def test_malformed_realistic_message_events_are_rejected(mutator):
    raw = callback("transport", {"action": "operator"})
    mutator(raw)
    with pytest.raises(ValidationError):
        parse_event(raw)


def test_operator_and_bot_callbacks_ack_once_and_persist(environment):
    db, client, adapter = environment
    assert adapter.handle_event(callback("op", {"action": "operator"})).response_type == "SWITCHED_TO_OPERATOR"
    assert client.callbacks == [{"event_id": "vk-callback-op", "user_id": 100, "peer_id": 100, "text": "Диалог передан оператору"}]
    assert client.sent[-1].text == SWITCHED_TO_OPERATOR_TEXT
    assert payloads(client.sent[-1].keyboard) == [{"action": "bot"}]
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "operator"

    assert adapter.handle_event(callback("bot", {"action": "bot"})).response_type == "SWITCHED_TO_BOT"
    assert len(client.callbacks) == 2
    assert client.callbacks[-1]["text"] == "Автоматический помощник включён"
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "bot"


def test_duplicate_callback_has_no_second_message_or_ack(environment):
    _, client, adapter = environment
    raw = callback("same", {"action": "operator"})
    assert adapter.handle_event(raw).status is AdapterStatus.PROCESSED
    assert adapter.handle_event(raw).status is AdapterStatus.DUPLICATE
    assert len(client.sent) == len(client.callbacks) == 1


def test_repeated_distinct_mode_callbacks_are_idempotent(environment):
    db, client, adapter = environment
    for event_id in ("operator-1", "operator-2"):
        assert adapter.handle_event(callback(event_id, {"action": "operator"})).status is AdapterStatus.PROCESSED
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "operator"
    for event_id in ("bot-1", "bot-2"):
        assert adapter.handle_event(callback(event_id, {"action": "bot"})).status is AdapterStatus.PROCESSED
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "bot"
    assert len(client.callbacks) == 4


@pytest.mark.parametrize("bad_payload", [
    None, {}, {"other": "operator"}, {"action": "unknown"},
    {"action": []},
    {"action": "operator", "nonce": "extra"},
    {"action": "clarification", "faq_id": True, "nonce": "x"},
    {"action": "clarification", "faq_id": 0, "nonce": "x"},
    {"action": "clarification", "faq_id": 1},
    {"action": "clarification", "faq_id": 1, "nonce": ""},
    {"action": "clarification", "faq_id": 1, "nonce": "x" * 129},
])
def test_invalid_callbacks_only_receive_safe_ack(environment, bad_payload):
    db, client, adapter = environment
    assert adapter.handle_event(callback(f"bad-{len(client.callbacks)}", bad_payload)).status is AdapterStatus.INVALID_EVENT
    assert client.sent == []
    assert client.callbacks[-1]["text"] == CALLBACK_REJECTED_ACK
    assert "token" not in json.dumps(client.callbacks[-1]).lower()
    assert connect(db).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_clarification_operator_invalidates_old_nonce_across_return(environment):
    db, client, adapter = environment
    assert adapter.handle_event(message("question", "для чего нужен электронный")).response_type == "CLARIFICATION"
    old = payloads(client.sent[-1].keyboard)[0]
    assert payloads(client.sent[-1].keyboard)[-1] == {"action": "operator"}
    assert adapter.handle_event(callback("handoff", {"action": "operator"})).response_type == "SWITCHED_TO_OPERATOR"
    assert connect(db).execute("SELECT COUNT(*) FROM pending_clarifications WHERE vk_user_id=100").fetchone()[0] == 0
    assert adapter.handle_event(callback("stale-operator", old)).status is AdapterStatus.NO_AUTOREPLY
    assert client.callbacks[-1]["text"] == CALLBACK_REJECTED_ACK
    assert adapter.handle_event(callback("return", {"action": "bot"})).response_type == "SWITCHED_TO_BOT"
    assert adapter.handle_event(callback("stale-bot", old)).response_type == "NOT_FOUND"
    assert client.callbacks[-1]["text"] == CALLBACK_REJECTED_ACK
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "bot"


def test_valid_clarification_is_acknowledged_once_and_nonce_is_consumed(environment):
    _, client, adapter = environment
    adapter.handle_event(message("offer-valid", "для чего нужен электронный"))
    offered = payloads(client.sent[-1].keyboard)[0]
    assert adapter.handle_event(callback("choose", offered)).response_type == "FAQ_ANSWER"
    assert client.callbacks[-1]["text"] == "Ответ выбран"
    assert len(client.callbacks) == 1
    assert adapter.handle_event(callback("choose-again", offered)).response_type == "NOT_FOUND"
    assert client.callbacks[-1]["text"] == CALLBACK_REJECTED_ACK


def test_cross_user_clarification_is_rejected(environment):
    db, client, adapter = environment
    adapter.handle_event(message("offer", "для чего нужен электронный", user=200))
    offered = payloads(client.sent[-1].keyboard)[0]
    assert adapter.handle_event(callback("steal", offered, user=201)).response_type == "NOT_FOUND"
    assert client.callbacks[-1]["text"] == CALLBACK_REJECTED_ACK
    assert connect(db).execute("SELECT COUNT(*) FROM pending_clarifications WHERE vk_user_id=200").fetchone()[0] == 1


def test_operator_mode_never_calls_search_and_records_incoming(populated_db, monkeypatch):
    logic = BotLogic(populated_db)
    logic.handle_mode_callback(300, "operator")
    monkeypatch.setattr(logic.search_service, "search", lambda _text: pytest.fail("FAQSearch called"))
    before = populated_db.execute("SELECT COUNT(*) FROM messages WHERE vk_user_id=300").fetchone()[0]
    assert logic.handle_message(300, "Мне нужна помощь человека").type is ResponseType.OPERATOR_MODE
    rows = populated_db.execute("SELECT direction,response_type FROM messages WHERE vk_user_id=300 ORDER BY id").fetchall()
    assert len(rows) == before + 1 and rows[-1]["direction"] == "incoming"


@pytest.mark.parametrize("command", ["оператор", "связаться с оператором", "позвать оператора", "живой оператор"])
def test_all_operator_text_fallbacks(command, populated_db):
    assert BotLogic(populated_db).handle_message(301, command).type is ResponseType.SWITCHED_TO_OPERATOR
    assert populated_db.execute("SELECT mode FROM users WHERE vk_user_id=301").fetchone()[0] == "operator"


@pytest.mark.parametrize("command", ["вернуться к боту", "бот", "вернуться в меню"])
def test_all_bot_text_fallbacks_clear_pending(command, populated_db):
    logic = BotLogic(populated_db)
    logic.handle_message(302, "для чего нужен электронный")
    logic.handle_mode_callback(302, "operator")
    # Create a pending row to prove that bot transition itself clears stale state.
    logic.interactions.save_clarification(302, [1], "stale")
    assert logic.handle_message(302, command).type is ResponseType.SWITCHED_TO_BOT
    assert populated_db.execute("SELECT mode FROM users WHERE vk_user_id=302").fetchone()[0] == "bot"
    assert populated_db.execute("SELECT COUNT(*) FROM pending_clarifications WHERE vk_user_id=302").fetchone()[0] == 0


def test_end_to_end_handoff_is_scoped_per_user(environment):
    db, client, adapter = environment
    assert adapter.handle_event(message("a-faq", "где находится институт", 401)).response_type == "FAQ_ANSWER"
    assert payloads(client.sent[-1].keyboard) == [{"action": "operator"}]
    assert adapter.handle_event(callback("a-op", {"action": "operator"}, 401)).response_type == "SWITCHED_TO_OPERATOR"
    sends = client.call_count
    assert adapter.handle_event(message("a-human", "Мне нужна помощь человека", 401)).status is AdapterStatus.NO_AUTOREPLY
    assert client.call_count == sends
    assert adapter.handle_event(message("b-faq", "почему учеба по субботам", 402)).response_type == "FAQ_ANSWER"
    assert adapter.handle_event(callback("a-bot", {"action": "bot"}, 401)).response_type == "SWITCHED_TO_BOT"
    assert adapter.handle_event(message("a-again", "почему учеба по субботам", 401)).response_type == "FAQ_ANSWER"
    modes = {row[0]: row[1] for row in connect(db).execute("SELECT vk_user_id,mode FROM users")}
    assert modes[401] == modes[402] == "bot"


def test_two_distinct_user_callbacks_do_not_lock_or_lose_state(environment):
    db, _, adapter = environment
    results = []
    threads = [
        threading.Thread(
            target=lambda user=user: results.append(
                adapter.handle_event(callback(f"concurrent-{user}", {"action": "operator"}, user))
            )
        )
        for user in (501, 502)
    ]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert [result.status for result in results].count(AdapterStatus.PROCESSED) == 2
    assert connect(db).execute("SELECT COUNT(*) FROM users WHERE mode='operator'").fetchone()[0] == 2


def test_ack_failure_does_not_rollback_state_or_repeat_main_message(environment):
    db, _, _ = environment

    class AckFailureClient(MockVKClient):
        def answer_callback(self, event_id, user_id, peer_id, text):
            raise TemporaryVKError("synthetic acknowledgement failure")

    client = AckFailureClient()
    adapter = VKAdapter(lambda: connect(db), client, VKConfig(rate_limit_per_minute=100), sleep=lambda _: None)
    assert adapter.handle_event(callback("ack-failure", {"action": "operator"})).status is AdapterStatus.PROCESSED
    assert client.call_count == 1
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "operator"


def test_callback_is_acknowledged_even_when_main_send_fails(environment):
    db, _, _ = environment

    class SendFailureClient(MockVKClient):
        def send_message(self, peer_id, text, keyboard=None):
            raise PermanentVKError("synthetic send failure")

    client = SendFailureClient()
    adapter = VKAdapter(lambda: connect(db), client, VKConfig(rate_limit_per_minute=100), sleep=lambda _: None)
    assert adapter.handle_event(callback("send-failure", {"action": "operator"})).status is AdapterStatus.ERROR
    assert len(client.callbacks) == 1
    assert connect(db).execute("SELECT mode FROM users WHERE vk_user_id=100").fetchone()[0] == "operator"
