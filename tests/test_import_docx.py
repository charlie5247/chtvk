from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from database import connect
from faq_repository import FAQRepository
from import_docx import extract_faq, import_faq


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
