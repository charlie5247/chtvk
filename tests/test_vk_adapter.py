import json
import logging
import random
import sqlite3
import string
import threading
from datetime import datetime, timedelta, timezone

import pytest

from database import connect
from import_docx import import_faq
from vk.adapter import AdapterStatus, CALLBACK_REJECTED_TEXT, VKAdapter
from vk.client import MockVKClient
from vk.config import VKConfig
from vk.exceptions import ConfigurationError, PermanentVKError, TemporaryVKError


def message(event_id, text, user=100, peer=None):
    return {"type": "message_new", "event_id": event_id, "object": {"message": {"from_id": user, "peer_id": peer or user, "text": text, "conversation_message_id": 1, "date": 1}}}


def callback(event_id, payload, user=100, peer=None):
    return {"type": "message_event", "event_id": event_id, "object": {"user_id": user, "peer_id": peer or user, "text": "", "payload": payload}}


@pytest.fixture()
def vk_env(tmp_path):
    db = tmp_path / "bot.db"
    import_faq("chat_bot.docx", db, tmp_path / "faq.json", tmp_path / "report.json")
    client = MockVKClient()
    config = VKConfig(db_path=str(db), rate_limit_per_minute=100)
    adapter = VKAdapter(lambda: connect(db), client, config, sleep=lambda _: None)
    return db, client, adapter


def test_mock_config_needs_no_token(monkeypatch):
    monkeypatch.delenv("VK_TOKEN", raising=False); monkeypatch.setenv("VK_MODE", "mock")
    assert VKConfig.from_env().token is None


def test_production_requires_secrets(monkeypatch):
    monkeypatch.delenv("VK_TOKEN", raising=False); monkeypatch.delenv("VK_GROUP_ID", raising=False); monkeypatch.setenv("VK_MODE", "production")
    with pytest.raises(ConfigurationError) as error:
        VKConfig.from_env()
    assert "secret-value" not in str(error.value)


def test_faq_end_to_end(vk_env):
    db, client, adapter = vk_env
    result = adapter.handle_event(message("faq-1", "почему учеба по субботам"))
    expected = connect(db).execute("SELECT answer FROM faq WHERE question='Почему есть занятия по субботам?'").fetchone()[0]
    assert result.response_type == "FAQ_ANSWER" and client.sent[0].text == expected


def test_not_found_has_operator_keyboard(vk_env):
    _, client, adapter = vk_env
    assert adapter.handle_event(message("nf-1", "сколько стоит обучение")).response_type == "NOT_FOUND"
    assert "operator" in client.sent[0].keyboard["buttons"][0][0]["action"]["payload"]


def test_clarification_callback_full_flow(vk_env):
    _, client, adapter = vk_env
    assert adapter.handle_event(message("cl-1", "для чего нужен электронный")).response_type == "CLARIFICATION"
    payload = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    result = adapter.handle_event(callback("cl-2", payload))
    assert result.response_type == "FAQ_ANSWER"
    assert client.sent[-1].text and client.sent[-1].text != CALLBACK_REJECTED_TEXT
    repeated = adapter.handle_event(callback("cl-3", payload))
    assert repeated.response_type == "NOT_FOUND"


def test_forged_other_user_expired_and_inactive_callbacks(vk_env):
    db, client, adapter = vk_env
    adapter.handle_event(message("c1", "для чего нужен электронный"))
    payload = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    forged = {"action": "clarification", "faq_id": 999999}
    assert adapter.handle_event(callback("c2", forged)).status is AdapterStatus.INVALID_EVENT
    assert client.sent[-1].text == CALLBACK_REJECTED_TEXT

    adapter.handle_event(message("c3", "для чего нужен электронный", user=101))
    assert adapter.handle_event(callback("c4", payload, user=101)).response_type == "NOT_FOUND"

    adapter.handle_event(message("c5", "для чего нужен электронный", user=102))
    current = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    c = connect(db); c.execute("UPDATE pending_clarifications SET expires_at=? WHERE vk_user_id=102", ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(timespec="seconds"),)); c.commit(); c.close()
    assert adapter.handle_event(callback("c6", current, user=102)).response_type == "NOT_FOUND"

    adapter.handle_event(message("c7", "для чего нужен электронный", user=103))
    current = json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    c = connect(db); c.execute("UPDATE faq SET active=0 WHERE id=?", (current["faq_id"],)); c.commit(); c.close()
    assert adapter.handle_event(callback("c8", current, user=103)).response_type == "NOT_FOUND"


def test_duplicate_event_has_one_send_and_history(vk_env):
    db, client, adapter = vk_env
    event = message("dup-1", "почему учеба по субботам")
    assert adapter.handle_event(event).status is AdapterStatus.PROCESSED
    assert adapter.handle_event(event).status is AdapterStatus.DUPLICATE
    assert client.call_count == 1
    c = connect(db); assert c.execute("SELECT COUNT(*) FROM messages WHERE vk_user_id=100").fetchone()[0] == 2; c.close()


def test_parallel_duplicate_event(vk_env):
    _, client, adapter = vk_env
    event = message("parallel-1", "почему учеба по субботам")
    results=[]
    threads=[threading.Thread(target=lambda: results.append(adapter.handle_event(event))) for _ in range(2)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert client.call_count == 1
    assert sorted(result.status.value for result in results) == ["DUPLICATE", "PROCESSED"]


def test_operator_flow_and_duplicate_switch(vk_env):
    _, client, adapter = vk_env
    event = message("op-1", "оператор")
    assert adapter.handle_event(event).response_type == "SWITCHED_TO_OPERATOR"
    assert adapter.handle_event(event).status is AdapterStatus.DUPLICATE
    calls = client.call_count
    assert adapter.handle_event(message("op-2", "почему учеба по субботам")).status is AdapterStatus.NO_AUTOREPLY
    assert client.call_count == calls
    assert adapter.handle_event(message("op-3", "вернуться к боту")).response_type == "SWITCHED_TO_BOT"


def test_rate_limit_and_long_input_bypass_logic(tmp_path):
    db=tmp_path/"b.db"; import_faq("chat_bot.docx",db,tmp_path/"e",tmp_path/"r")
    client=MockVKClient(); adapter=VKAdapter(lambda:connect(db),client,VKConfig(rate_limit_per_minute=1,max_input_length=10,db_path=str(db)),sleep=lambda _:None)
    adapter.handle_event(message("r1", "погода")); assert adapter.handle_event(message("r2", "другой вопрос")).status is AdapterStatus.RATE_LIMITED
    c=connect(db); assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2; c.close()
    adapter2=VKAdapter(lambda:connect(db),client,VKConfig(rate_limit_per_minute=100,max_input_length=10,db_path=str(db)),sleep=lambda _:None)
    for index, size in enumerate((5001,10001,100000)):
        assert adapter2.handle_event(message(f"long-{index}", "я"*size, user=200+index)).status is AdapterStatus.INPUT_TOO_LONG


@pytest.mark.parametrize("raw,status", [({},AdapterStatus.INVALID_EVENT), ({"type":"wall_post_new"},AdapterStatus.UNSUPPORTED_EVENT), (None,AdapterStatus.INVALID_EVENT), ({"type":"message_new","event_id":"","object":{}},AdapterStatus.INVALID_EVENT)])
def test_invalid_and_unsupported_events(raw,status,vk_env):
    assert vk_env[2].handle_event(raw).status is status


def test_empty_input_is_safe(vk_env):
    assert vk_env[2].handle_event(message("empty", "")).response_type == "NOT_FOUND"


def test_sqlite_open_error_is_boundary(vk_env):
    adapter=VKAdapter(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("broken")),vk_env[1],VKConfig())
    assert adapter.handle_event(message("db-error", "test")).status is AdapterStatus.ERROR


class FlakyClient(MockVKClient):
    def __init__(self, error, failures): super().__init__(); self.error=error; self.failures=failures; self.attempts=0
    def send_message(self, peer_id, text, keyboard=None):
        self.attempts += 1
        if self.attempts <= self.failures: raise self.error
        super().send_message(peer_id,text,keyboard)


def test_network_timeout_retries_are_bounded(vk_env):
    client=FlakyClient(TimeoutError(),2); adapter=VKAdapter(lambda:connect(vk_env[0]),client,VKConfig(rate_limit_per_minute=100),sleep=lambda _:None)
    assert adapter.handle_event(message("retry", "почему учеба по субботам")).status is AdapterStatus.PROCESSED
    assert client.attempts == 3


def test_permanent_error_has_no_retry(vk_env):
    client=FlakyClient(PermanentVKError("denied"),10); adapter=VKAdapter(lambda:connect(vk_env[0]),client,VKConfig(rate_limit_per_minute=100),sleep=lambda _:None)
    assert adapter.handle_event(message("permanent", "почему учеба по субботам")).status is AdapterStatus.ERROR
    assert client.attempts == 1


def test_logs_do_not_contain_token(monkeypatch,caplog):
    secret="super-private-token"; monkeypatch.setenv("VK_MODE","production"); monkeypatch.setenv("VK_TOKEN",secret); monkeypatch.setenv("VK_GROUP_ID","1")
    with caplog.at_level(logging.INFO): VKConfig.from_env()
    assert secret not in caplog.text


def test_random_payload_smoke(vk_env):
    adapter=vk_env[2]; random.seed(7)
    values=[None,1,"x",[],{},True]
    for index in range(50):
        raw={"type":random.choice(values),"event_id":random.choice(values),"object":random.choice(values)}
        assert adapter.handle_event(raw).status in set(AdapterStatus)
    for index in range(10):
        text="".join(random.choice(string.printable+"Привет🙂\n") for _ in range(index*20))
        assert adapter.handle_event(message(f"fuzz-{index}",text,user=500+index)).status in set(AdapterStatus)
