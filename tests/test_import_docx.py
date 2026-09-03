from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from database import connect
from faq_repository import FAQRepository
import json

import pytest

from faq_search import Confidence, FAQSearch
from import_docx import build_source_diff, extract_faq, import_faq, metadata_for


def make_docx(tmp_path: Path, paragraphs: list[str]) -> Path:
    path = tmp_path / "input.docx"
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>' for text in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return path


def test_normal_question_answer_and_russian(tmp_path):
    pairs, issues = extract_faq(make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ: Русский текст ответа"])); assert pairs == [("Посещение занятий.", "Русский текст ответа")]; assert not issues


def test_numbered_answer(tmp_path):
    pairs, _ = extract_faq(make_docx(tmp_path, ["Вопрос: Посещение занятий.", "1. Ответ: Текст"])); assert pairs[0][1] == "Текст"


def test_answer_without_marker(tmp_path):
    pairs, issues = extract_faq(make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ без метки"])); assert pairs[0][1] == "Ответ без метки"; assert issues[0]["type"] == "missing_answer_marker"


def test_multiline_answer(tmp_path):
    pairs, _ = extract_faq(make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ: Первая строка", "Вторая строка"])); assert pairs[0][1] == "Первая строка\nВторая строка"


def test_link_is_preserved(tmp_path):
    pairs, _ = extract_faq(make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ: https://example.org/path"])); assert "https://example.org/path" in pairs[0][1]


def test_email_is_preserved(tmp_path):
    pairs, _ = extract_faq(make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ: mail@example.org"])); assert "mail@example.org" in pairs[0][1]


def test_repeated_import_has_no_duplicates(tmp_path):
    db, export, report = tmp_path / "bot.db", tmp_path / "export.json", tmp_path / "report.json"
    doc = make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ: Русский ответ"])
    import_faq(doc, db, export, report); import_faq(doc, db, export, report)
    connection = connect(db)
    assert FAQRepository(connection).count() == 1
    assert FAQRepository(connection).all()[0]["answer"] == "Русский ответ"


def test_reimport_preserves_manual_priority_and_active(tmp_path):
    db, export, report = tmp_path / "bot.db", tmp_path / "export.json", tmp_path / "report.json"
    doc = make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ: Первый ответ"])
    import_faq(doc, db, export, report)
    connection = connect(db)
    connection.execute("UPDATE faq SET priority=777, active=0"); connection.commit(); connection.close()
    import_faq(doc, db, export, report)
    row = connect(db).execute("SELECT priority,active FROM faq").fetchone()
    assert tuple(row) == (777, 0)


def test_updated_docx_has_complete_reviewed_metadata():
    pairs, _ = extract_faq("chat_bot_updated.docx")
    assert len(pairs) == 14
    assert all(metadata_for(question) for question, _ in pairs)


def test_reviewed_question_rename_preserves_identity_and_manual_fields(tmp_path):
    db = tmp_path / "bot.db"
    import_faq("chat_bot.docx", db, tmp_path / "old.json", tmp_path / "old-report.json")
    connection = connect(db)
    old = connection.execute(
        "SELECT id,answer,category,keywords,aliases FROM faq WHERE question=?",
        ("Как связаться с преподавателем?",),
    ).fetchone()
    connection.execute("UPDATE faq SET priority=777,active=0 WHERE id=?", (old["id"],))
    connection.commit(); connection.close()

    import_faq("chat_bot_updated.docx", db, tmp_path / "new.json", tmp_path / "new-report.json", tmp_path / "diff.json")
    connection = connect(db)
    renamed = connection.execute("SELECT * FROM faq WHERE id=?", (old["id"],)).fetchone()
    updated_answer = dict(extract_faq("chat_bot_updated.docx")[0])["Как связаться или найти преподавателя?"]
    assert renamed["question"] == "Как связаться или найти преподавателя?"
    assert renamed["answer"] == updated_answer
    assert (renamed["priority"], renamed["active"]) == (777, 0)
    assert renamed["category"] == old["category"] == "Преподаватели"
    assert json.loads(renamed["keywords"]) == json.loads(old["keywords"])
    assert "Как связаться с преподавателем?" in json.loads(renamed["aliases"])
    assert connection.execute("SELECT COUNT(*) FROM faq").fetchone()[0] == 14
    connection.close()


def test_updated_import_is_idempotent_and_keeps_renamed_id(tmp_path):
    db = tmp_path / "bot.db"
    paths = (tmp_path / "export.json", tmp_path / "report.json", tmp_path / "diff.json")
    import_faq("chat_bot.docx", db, *paths)
    before = connect(db).execute("SELECT id FROM faq WHERE question='Как связаться с преподавателем?'").fetchone()[0]
    import_faq("chat_bot_updated.docx", db, *paths)
    import_faq("chat_bot_updated.docx", db, *paths)
    connection = connect(db)
    rows = connection.execute("SELECT id FROM faq WHERE question LIKE 'Как связаться%преподавателя?'").fetchall()
    assert [row[0] for row in rows] == [before]
    assert connection.execute("SELECT COUNT(*) FROM faq").fetchone()[0] == 14
    connection.close()


def test_updated_answers_replace_old_source_answers(tmp_path):
    db = tmp_path / "bot.db"
    paths = (tmp_path / "export.json", tmp_path / "report.json", tmp_path / "diff.json")
    import_faq("chat_bot.docx", db, *paths)
    import_faq("chat_bot_updated.docx", db, *paths)
    expected = dict(extract_faq("chat_bot_updated.docx")[0])["Для чего нужен Электронный справочник?"]
    actual = connect(db).execute("SELECT answer FROM faq WHERE question='Для чего нужен Электронный справочник?'").fetchone()[0]
    assert actual == expected


def test_missing_source_faq_is_reported_and_not_deleted(tmp_path):
    db = tmp_path / "bot.db"
    paths = (tmp_path / "export.json", tmp_path / "report.json", tmp_path / "diff.json")
    import_faq("chat_bot.docx", db, *paths)
    short_doc = make_docx(tmp_path, ["Вопрос: Посещение занятий.", "Ответ: Новый ответ"])
    import_faq(short_doc, db, *paths)
    assert connect(db).execute("SELECT COUNT(*) FROM faq").fetchone()[0] == 14
    diff = json.loads(paths[2].read_text(encoding="utf-8"))
    assert diff["summary"]["REMOVED_FROM_SOURCE"] == 13


def test_unknown_metadata_mapping_blocks_before_database_update(tmp_path):
    db = tmp_path / "bot.db"
    doc = make_docx(tmp_path, ["Вопрос: Полностью неизвестный вопрос?", "Ответ: Ответ"])
    with pytest.raises(ValueError, match="Нет проверенных метаданных"):
        import_faq(doc, db, tmp_path / "export.json", tmp_path / "report.json", tmp_path / "diff.json")
    assert not db.exists()


def test_renamed_question_searches_by_new_question_and_old_alias(tmp_path):
    db = tmp_path / "bot.db"
    import_faq("chat_bot_updated.docx", db, tmp_path / "export.json", tmp_path / "report.json", tmp_path / "diff.json")
    connection = connect(db)
    for query, match_type in (("Как связаться или найти преподавателя?", "exact_question"), ("Как связаться с преподавателем?", "exact_alias")):
        result = FAQSearch(connection).search(query)
        assert result.confidence is Confidence.HIGH
        assert result.match_type == match_type
        assert result.faq["question"] == "Как связаться или найти преподавателя?"


@pytest.mark.parametrize("query", ["где преподаватель", "контакты", "найти сотрудника института"])
def test_updated_import_does_not_create_false_high_confidence(tmp_path, query):
    db = tmp_path / "bot.db"
    import_faq("chat_bot_updated.docx", db, tmp_path / "export.json", tmp_path / "report.json", tmp_path / "diff.json")
    assert FAQSearch(connect(db)).search(query).confidence is not Confidence.HIGH


def test_source_diff_classifies_reviewed_rename_and_all_answers():
    diff = build_source_diff(extract_faq("chat_bot.docx")[0], extract_faq("chat_bot_updated.docx")[0])
    assert diff["summary"] == {
        "ADDED": 0, "MODIFIED_ANSWER": 8, "MODIFIED_QUESTION": 1,
        "MODIFIED_METADATA": 0, "REMOVED_FROM_SOURCE": 0, "UNCHANGED": 5,
        "POSSIBLE_DUPLICATE": 0, "AMBIGUOUS": 0,
    }
