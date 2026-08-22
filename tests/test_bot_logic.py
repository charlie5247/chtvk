import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bot_logic import BotLogic, ResponseType
from database import connect
from faq_search import Confidence, FAQSearch, normalize_text
from import_docx import import_faq
from user_repository import UserRepository


@pytest.fixture()
def populated_db(tmp_path):
    path = tmp_path / "bot.db"
    import_faq("chat_bot.docx", path, tmp_path / "faq.json", tmp_path / "report.json")
    connection = connect(path)
    yield connection
    connection.close()


def test_exact_question(populated_db):
    result = FAQSearch(populated_db).search("Почему есть занятия по субботам?")
    assert result.match_type == "exact_question" and result.confidence is Confidence.HIGH


def test_exact_alias(populated_db):
    result = FAQSearch(populated_db).search("где находится корпус института")
    assert result.match_type == "exact_alias" and result.faq["id"] == 1


def test_keywords(populated_db):
    result = FAQSearch(populated_db).search("пожар эвакуация безопасность")
    assert result.match_type == "keywords" and result.faq["question"] == "Что делать в случае пожара?"


@pytest.mark.parametrize("text", ["ПОЧЕМУ ЕСТЬ ЗАНЯТИЯ ПО СУББОТАМ", "Почему есть занятия по субботам!!!"])
def test_case_and_punctuation(populated_db, text):
    assert FAQSearch(populated_db).search(text).confidence is Confidence.HIGH


def test_yo_normalization():
    assert normalize_text("  Ёлка, ещё! ") == "елка еще"


def test_natural_wording(populated_db):
    result = FAQSearch(populated_db).search("не сдал сессию что делать")
    assert result.faq["question"] == "Что делать если я не сдал сессию?"


@pytest.mark.parametrize(("text", "question"), [
    ("где находится институт", "Корпус института экономики и управления"),
    ("почему учеба по субботам", "Почему есть занятия по субботам?"),
    ("как найти преподавателя", "Как связаться с преподавателем?"),
    ("как получить общагу", "Как получить место в общежитии?"),
])
def test_required_real_queries(populated_db, text, question):
    result = FAQSearch(populated_db).search(text)
    assert result.confidence is Confidence.HIGH
    assert result.faq["question"] == question


@pytest.mark.parametrize("text", ["сколько стоит обучение", "какая сегодня погода"])
def test_weak_match_is_low(populated_db, text):
    assert FAQSearch(populated_db).search(text).confidence is Confidence.LOW


def test_similar_faqs_require_clarification(populated_db):
    response = BotLogic(populated_db).handle_message(1, "для чего нужен электронный")
    assert response.type is ResponseType.CLARIFICATION
    assert 1 <= len(response.options) <= 3
    selected = BotLogic(populated_db).select_clarification(1, response.options[0]["faq_id"], response.options[0]["nonce"])
    assert selected.type is ResponseType.FAQ_ANSWER
    assert BotLogic(populated_db).select_clarification(1, response.options[0]["faq_id"], response.options[0]["nonce"]).type is ResponseType.NOT_FOUND


def test_clarification_rejects_unoffered_other_user_expired_and_inactive(populated_db):
    bot = BotLogic(populated_db)
    response = bot.handle_message(10, "для чего нужен электронный")
    offered = response.options[0]["faq_id"]
    assert bot.select_clarification(11, offered, response.options[0]["nonce"]).type is ResponseType.NOT_FOUND
    assert bot.select_clarification(10, 999999, response.options[0]["nonce"]).type is ResponseType.NOT_FOUND

    response = bot.handle_message(10, "для чего нужен электронный")
    offered = response.options[0]["faq_id"]
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    populated_db.execute("UPDATE pending_clarifications SET expires_at=? WHERE vk_user_id=10", (expired,)); populated_db.commit()
    assert bot.select_clarification(10, offered, response.options[0]["nonce"]).type is ResponseType.NOT_FOUND

    response = bot.handle_message(10, "для чего нужен электронный")
    offered = response.options[0]["faq_id"]
    populated_db.execute("UPDATE faq SET active=0 WHERE id=?", (offered,)); populated_db.commit()
    assert bot.select_clarification(10, offered, response.options[0]["nonce"]).type is ResponseType.NOT_FOUND


def test_new_question_invalidates_clarification(populated_db):
    bot = BotLogic(populated_db)
    response = bot.handle_message(12, "для чего нужен электронный")
    offered = response.options[0]["faq_id"]
    bot.handle_message(12, "какая сегодня погода")
    assert bot.select_clarification(12, offered, response.options[0]["nonce"]).type is ResponseType.NOT_FOUND


def test_unknown_is_saved_and_deduplicated(populated_db):
    bot = BotLogic(populated_db)
    assert bot.handle_message(2, "какая сегодня погода").type is ResponseType.NOT_FOUND
    assert bot.handle_message(2, "какая сегодня погода").type is ResponseType.NOT_FOUND
    assert populated_db.execute("SELECT COUNT(*) FROM unknown_questions WHERE vk_user_id=2").fetchone()[0] == 1


def test_unknown_normalized_dedup_window_and_users(populated_db):
    bot = BotLogic(populated_db)
    for text in ("Ещё погода?", "еще погода", "  ЕЩЁ   ПОГОДА!!!  "):
        bot.handle_message(20, text)
    assert populated_db.execute("SELECT COUNT(*) FROM unknown_questions WHERE vk_user_id=20").fetchone()[0] == 1
    bot.handle_message(21, "еще погода")
    assert populated_db.execute("SELECT COUNT(*) FROM unknown_questions WHERE normalized_text='еще погода'").fetchone()[0] == 2
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(timespec="seconds")
    populated_db.execute("UPDATE unknown_questions SET created_at=? WHERE vk_user_id=20", (old,)); populated_db.commit()
    bot.handle_message(20, "еще погода")
    assert populated_db.execute("SELECT COUNT(*) FROM unknown_questions WHERE vk_user_id=20").fetchone()[0] == 2


def test_new_user_defaults_to_bot(populated_db):
    assert UserRepository(populated_db).get_or_create_user(3).mode == "bot"


def test_operator_round_trip(populated_db):
    bot = BotLogic(populated_db)
    assert bot.handle_message(4, "оператор").type is ResponseType.SWITCHED_TO_OPERATOR
    assert bot.operator.is_operator_mode(4)
    assert bot.handle_message(4, "почему есть занятия по субботам").type is ResponseType.OPERATOR_MODE
    assert bot.handle_message(4, "вернуться к боту").type is ResponseType.SWITCHED_TO_BOT
    assert bot.handle_message(4, "почему есть занятия по субботам").type is ResponseType.FAQ_ANSWER


def test_operator_mode_persists_and_commands_are_idempotent(populated_db):
    first = BotLogic(populated_db)
    first.handle_message(30, "оператор"); first.handle_message(30, "оператор")
    restarted = BotLogic(populated_db)
    assert restarted.handle_message(30, "неизвестная команда").type is ResponseType.OPERATOR_MODE
    restarted.handle_message(30, "вернуться к боту")
    assert restarted.handle_message(30, "вернуться к боту").type is ResponseType.SWITCHED_TO_BOT


@pytest.mark.parametrize("text", ["", " ", ".", "???", "🙂🔥", "123456", "https://example.org", "mail@example.org", "строка\nEnglish mixed", "я" * 5000])
def test_strange_inputs_do_not_crash_or_answer(populated_db, text):
    response = BotLogic(populated_db).handle_message(40, text)
    assert response.type is ResponseType.NOT_FOUND


@pytest.mark.parametrize("text", ["где", "сессия", "документы", "электронный", "преподаватель", "общежитие", "пожар", "не сдал", "пропуск", "карточка", "как"])
def test_short_or_general_queries_never_auto_answer(populated_db, text):
    assert BotLogic(populated_db).handle_message(41, text).type is not ResponseType.FAQ_ANSWER


def test_inactive_faq_is_excluded_from_search(populated_db):
    populated_db.execute("UPDATE faq SET active=0 WHERE question='Почему есть занятия по субботам?'"); populated_db.commit()
    assert FAQSearch(populated_db).search("почему учеба по субботам").faq is None


def test_equal_scores_are_stable_and_priority_breaks_tie(populated_db):
    payload = '["одинаковый псевдоним"]'
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for question, priority in (("Тестовый вопрос А", 100), ("Тестовый вопрос Б", 200)):
        populated_db.execute(
            "INSERT INTO faq(category,question,answer,keywords,aliases,priority,active,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
            ("Тест", question, question, '["тест"]', payload, priority, now, now),
        )
    populated_db.commit()
    result = FAQSearch(populated_db).search("одинаковый псевдоним")
    assert result.confidence is Confidence.MEDIUM
    assert result.alternatives[0].faq["question"] == "Тестовый вопрос Б"


def test_privacy_delete_removes_user_data(populated_db):
    bot = BotLogic(populated_db)
    bot.handle_message(60, "какая сегодня погода")
    bot.interactions.auto_commit = True
    bot.interactions.delete_user_data(60)
    for table in ("users", "messages", "unknown_questions", "pending_clarifications"):
        assert populated_db.execute(f"SELECT COUNT(*) FROM {table} WHERE vk_user_id=60").fetchone()[0] == 0


def test_message_transaction_rolls_back_on_sqlite_error(populated_db, monkeypatch):
    bot = BotLogic(populated_db)
    original = bot.interactions.save_message
    def fail_outgoing(*args, **kwargs):
        if args[1] == "outgoing":
            raise sqlite3.OperationalError("simulated")
        return original(*args, **kwargs)
    monkeypatch.setattr(bot.interactions, "save_message", fail_outgoing)
    with pytest.raises(sqlite3.OperationalError):
        bot.handle_message(50, "почему учеба по субботам")
    assert populated_db.execute("SELECT COUNT(*) FROM messages WHERE vk_user_id=50").fetchone()[0] == 0


def test_message_history_records_both_directions(populated_db):
    BotLogic(populated_db).handle_message(5, "почему учеба по субботам")
    rows = populated_db.execute("SELECT direction, response_type, faq_id FROM messages WHERE vk_user_id=5 ORDER BY id").fetchall()
    assert [row["direction"] for row in rows] == ["incoming", "outgoing"]
    assert rows[1]["response_type"] == "FAQ_ANSWER"
    assert rows[1]["faq_id"] is not None


def test_all_faq_remain_after_reimport(tmp_path):
    db = tmp_path / "bot.db"
    args = ("chat_bot.docx", db, tmp_path / "faq.json", tmp_path / "report.json")
    import_faq(*args); import_faq(*args)
    connection = connect(db)
    assert connection.execute("SELECT COUNT(*) FROM faq").fetchone()[0] == 14
