import json
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from database import connect
from import_docx import import_faq
from interaction_repository import InteractionRepository
from vk.adapter import AdapterStatus, VKAdapter
from vk.client import MockVKClient
from vk.config import VKConfig
from vk.deduplication import EventDeduplicator
from vk.events import parse_event
from vk.exceptions import ValidationError
from vk.keyboards import clarification_keyboard
from vk.rate_limit import RateLimiter

from test_vk_adapter import callback, message


@pytest.fixture()
def system(tmp_path):
    db = tmp_path / "audit.db"
    import_faq("chat_bot.docx", db, tmp_path / "export.json", tmp_path / "report.json")
    client = MockVKClient()
    adapter = VKAdapter(lambda: connect(db), client, VKConfig(rate_limit_per_minute=500, db_path=str(db)), sleep=lambda _: None)
    return db, client, adapter


@pytest.mark.parametrize("changes", [
    {"event_id": None}, {"event_id": ""}, {"user": 0}, {"user": -1}, {"user": True},
    {"peer": None}, {"text": None}, {"text": 1}, {"date": "now"}, {"payload": []},
])
def test_event_parser_rejects_invalid_fields(changes):
    raw = message("valid", "text")
    msg = raw["object"]["message"]
    if "event_id" in changes:
        raw["event_id"] = changes["event_id"]
    if "user" in changes:
        msg["from_id"] = changes["user"]
    if "peer" in changes:
        msg["peer_id"] = changes["peer"]
    if "text" in changes:
        msg["text"] = changes["text"]
    if "date" in changes:
        msg["date"] = changes["date"]
    if "payload" in changes:
        msg["payload"] = changes["payload"]
    with pytest.raises(ValidationError):
        parse_event(raw)


def test_event_parser_allows_unknown_fields_and_rejects_huge_malformed(system):
    raw = message("extra", "ok"); raw["unknown"] = {"large": "x" * 100_000}
    assert parse_event(raw).text == "ok"
    malformed = {"type": "message_new", "event_id": "huge", "object": {"message": {"junk": "x" * 1_000_000}}}
    assert system[2].handle_event(malformed).status is AdapterStatus.INVALID_EVENT


def test_dedup_status_ttl_cleanup_and_reclaim(tmp_path):
    db = tmp_path / "dedup.db"; connection = connect(db); dedup = EventDeduplicator(connection, 60)
    assert dedup.claim("done"); dedup.finish("done"); assert not dedup.claim("done")
    assert dedup.claim("failed"); dedup.finish("failed", "FAILED"); assert not dedup.claim("failed")
    assert dedup.claim("processing"); assert not dedup.claim("processing")
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    connection.execute("UPDATE processed_vk_events SET expires_at=? WHERE event_id='processing'", (expired,)); connection.commit()
    assert dedup.claim("processing")
    connection.execute("UPDATE processed_vk_events SET expires_at=? WHERE event_id='failed'", (expired,)); connection.commit()
    assert dedup.cleanup() == 1


def test_retention_cleans_messages_unknown_and_clarifications(system):
    db, _, adapter = system
    adapter.handle_event(message("ret-1", "какая погода", 808))
    adapter.handle_event(message("ret-2", "для чего нужен электронный", 809))
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat(timespec="seconds")
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    connection = connect(db)
    connection.execute("UPDATE messages SET created_at=? WHERE vk_user_id=808", (old,))
    connection.execute("UPDATE unknown_questions SET created_at=? WHERE vk_user_id=808", (old,))
    connection.execute("UPDATE pending_clarifications SET expires_at=? WHERE vk_user_id=809", (expired,))
    connection.commit()
    repository = InteractionRepository(connection)
    assert repository.purge_messages_before(datetime.now(timezone.utc).isoformat(timespec="seconds")) >= 2
    assert repository.purge_unknown_before(datetime.now(timezone.utc).isoformat(timespec="seconds")) == 1
    assert repository.purge_expired_clarifications() == 1
    connection.close()


def test_rate_limiter_boundary_users_window_and_threads():
    now = [100.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.allow(1) and limiter.allow(1) and not limiter.allow(1)
    assert limiter.allow(2)
    now[0] += 61
    assert limiter.allow(1)
    concurrent = RateLimiter(10)
    results=[]; threads=[threading.Thread(target=lambda: results.append(concurrent.allow(9))) for _ in range(20)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sum(results) == 10


@pytest.mark.parametrize("size,expected", [(100, "NOT_FOUND"), (4096, "NOT_FOUND"), (4097, "INPUT_TOO_LONG")])
def test_exact_input_boundaries(tmp_path, size, expected):
    db=tmp_path/"limit.db"; import_faq("chat_bot.docx",db,tmp_path/"e",tmp_path/"r")
    adapter=VKAdapter(lambda:connect(db),MockVKClient(),VKConfig(rate_limit_per_minute=10,max_input_length=4096),sleep=lambda _:None)
    result=adapter.handle_event(message(f"size-{size}","x"*size))
    assert (result.response_type or result.status.value) == expected


def test_full_user_journey_and_persistence(system):
    db, client, adapter = system; user=1001
    assert adapter.handle_event(message("j1","где находится институт",user)).response_type == "FAQ_ANSWER"
    c=connect(db); expected=c.execute("SELECT answer FROM faq WHERE id=1").fetchone()[0]; c.close()
    assert client.sent[-1].text == expected
    assert adapter.handle_event(message("j2","сколько стоит обучение",user)).response_type == "NOT_FOUND"
    assert adapter.handle_event(message("j3","для чего нужен электронный",user)).response_type == "CLARIFICATION"
    payload=json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    assert adapter.handle_event(callback("j4",payload,user)).response_type == "FAQ_ANSWER"
    assert adapter.handle_event(message("j5","оператор",user)).response_type == "SWITCHED_TO_OPERATOR"
    sent=client.call_count; assert adapter.handle_event(message("j6","пожар",user)).status is AdapterStatus.NO_AUTOREPLY; assert client.call_count == sent
    assert adapter.handle_event(message("j7","вернуться к боту",user)).response_type == "SWITCHED_TO_BOT"
    assert adapter.handle_event(message("j8","почему пары в субботу",user)).response_type == "FAQ_ANSWER"
    c=connect(db)
    assert c.execute("SELECT mode FROM users WHERE vk_user_id=?",(user,)).fetchone()[0] == "bot"
    assert c.execute("SELECT COUNT(*) FROM unknown_questions WHERE vk_user_id=?",(user,)).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM pending_clarifications WHERE vk_user_id=?",(user,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM processed_vk_events").fetchone()[0] == 8
    c.close()


def test_three_users_states_do_not_mix(system):
    db, _, adapter = system
    adapter.handle_event(message("m1","оператор",1001))
    adapter.handle_event(message("m2","для чего нужен электронный",1002))
    adapter.handle_event(message("m3","почему пары в субботу",1003))
    c=connect(db)
    assert c.execute("SELECT mode FROM users WHERE vk_user_id=1001").fetchone()[0] == "operator"
    assert c.execute("SELECT COUNT(*) FROM pending_clarifications WHERE vk_user_id=1002").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM pending_clarifications WHERE vk_user_id IN (1001,1003)").fetchone()[0] == 0
    c.close()


def test_operator_switch_invalidates_callback(system):
    _, client, adapter = system
    adapter.handle_event(message("o1","для чего нужен электронный",700))
    payload=json.loads(client.sent[-1].keyboard["buttons"][0][0]["action"]["payload"])
    adapter.handle_event(message("o2","оператор",700))
    assert adapter.handle_event(callback("o3",payload,700)).status is AdapterStatus.NO_AUTOREPLY
    adapter.handle_event(message("o4","вернуться к боту",700))
    assert adapter.handle_event(callback("o5",payload,700)).response_type == "NOT_FOUND"


def test_keyboard_constraints_and_no_duplicate_options():
    keyboard=clarification_keyboard([{"question":"Очень длинный русский текст "*5,"faq_id":1,"nonce":"n"},{"question":"duplicate","faq_id":1,"nonce":"n"},{"question":"B","faq_id":2,"nonce":"m"}])
    assert len(keyboard["buttons"]) == 3
    action=keyboard["buttons"][0][0]["action"]
    assert len(action["label"]) <= 40 and json.loads(action["payload"]) == {"action":"clarification","faq_id":1,"nonce":"n"}
    assert json.loads(keyboard["buttons"][-1][0]["action"]["payload"]) == {"action": "operator"}


def test_mock_client_concurrent_storage():
    client=MockVKClient(); threads=[threading.Thread(target=client.send_message,args=(i,"x")) for i in range(100)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert client.call_count == 100 and {m.peer_id for m in client.sent} == set(range(100))


def test_unexpected_client_error_is_contained(system):
    class Broken(MockVKClient):
        def send_message(self,*args,**kwargs): raise RuntimeError("synthetic")
    adapter=VKAdapter(lambda:connect(system[0]),Broken(),VKConfig(rate_limit_per_minute=10),sleep=lambda _:None)
    assert adapter.handle_event(message("unexpected","пожар")).status is AdapterStatus.ERROR


def test_1000_random_malformed_events_never_escape(system):
    random.seed(20260821); adapter=system[2]
    atoms=[None,True,False,0,-1,1,"", "x", [], {}, {"nested":"x"*1000}]
    for _ in range(1000):
        raw=random.choice(atoms)
        if random.random() < .7:
            raw={"type":random.choice(atoms),"event_id":random.choice(atoms),"object":random.choice(atoms),"extra":random.choice(atoms)}
        assert adapter.handle_event(raw).status in set(AdapterStatus)
