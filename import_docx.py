"""Import FAQ entries from chat_bot.docx and export a reviewable JSON copy."""

import argparse
import json
import re
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from database import connect
from faq_repository import FAQRepository
from models import FAQ


QUESTION_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?Вопрос\s*:\s*(.+?)\s*$", re.I)
ANSWER_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?Ответ\s*:\s*(.*)$", re.I | re.S)
MAX_DOCUMENT_XML_BYTES = 5 * 1024 * 1024

# Metadata is deliberately limited to paraphrases and concepts present in each question/answer.
METADATA = {
    "Корпус института экономики и управления": ("Институт", ["корпус", "адрес", "пропуск", "охрана"], ["где находится корпус института", "где находится институт", "адрес института экономики и управления", "как пройти в корпус института"]),
    "Посещение занятий.": ("Учебный процесс", ["занятия", "посещение", "отсутствие", "пропуск"], ["обязательно ли посещать занятия", "можно ли пропускать занятия", "причины отсутствия на занятиях"]),
    "Студенческое самоуправление.": ("Студенческая жизнь", ["студенческое самоуправление", "студсовет", "инициативы"], ["что такое студенческое самоуправление", "как узнать о студсовете", "чем занимается студсовет"]),
    "Как зарегистрироваться в электронной образовательной среде Герценовского университета?": ("Электронные сервисы", ["регистрация", "электронная среда", "ЕИС", "идентификатор"], ["как зарегистрировать ЕИС", "регистрация в электронной среде", "как получить единый идентификатор студента"]),
    "Для чего нужен Электронный справочник?": ("Электронные сервисы", ["электронный справочник", "личный кабинет", "расписание", "заявления", "справки"], ["что есть в электронном справочнике", "зачем нужен электронный справочник", "где найти личный кабинет обучающегося"]),
    "Для чего нужно Электронное портфолио обучающихся?": ("Электронные сервисы", ["электронное портфолио", "достижения", "обучающиеся"], ["зачем нужно электронное портфолио", "что такое электронное портфолио", "где хранить данные о достижениях"]),
    "Для чего нужен Центр дистанционной поддержки обучения?": ("Электронные сервисы", ["дистанционное обучение", "LMS", "Moodle", "учебные курсы"], ["зачем нужен центр дистанционного обучения", "что такое центр дистанционной поддержки", "где размещены материалы и задания"]),
    "Для чего нужен Электронный атлас?": ("Электронные сервисы", ["электронный атлас", "образовательные программы", "преподаватели", "кафедры"], ["зачем нужен электронный атлас", "что можно найти в электронном атласе", "где посмотреть сведения о программах и кафедрах"]),
    "Как получить место в общежитии?": ("Общежитие", ["общежитие", "место", "заселение", "иногородние"], ["как заселиться в общежитие", "кому дают общежитие", "куда обратиться по поводу общежития", "как получить общагу"]),
    "Почему есть занятия по субботам?": ("Учебный процесс", ["занятия", "суббота", "шестидневная неделя", "расписание"], ["почему учимся в субботу", "почему пары в субботу", "суббота учебный день", "почему учеба по субботам"]),
    "Что делать если я не сдал сессию?": ("Академическая задолженность", ["сессия", "академическая задолженность", "пересдача", "аттестация"], ["что делать с академической задолженностью", "как пересдать сессию", "сколько попыток на пересдачу", "не сдал сессию что делать"]),
    "Что делать в случае пожара?": ("Безопасность", ["пожар", "эвакуация", "безопасность", "план эвакуации"], ["что делать при пожаре", "как эвакуироваться при пожаре", "действия во время пожара"]),
    "Что делать в случае террористической опасности?": ("Безопасность", ["террористическая опасность", "тревога", "укрытие", "безопасность"], ["что делать при террористической угрозе", "действия при террористической опасности", "как вести себя во время тревоги"]),
    "Как связаться с преподавателем?": ("Преподаватели", ["преподаватель", "контакты", "электронный атлас", "переписка"], ["где найти контакты преподавателя", "как написать преподавателю", "как обратиться к преподавателю", "как найти преподавателя"]),
}


def extract_faq(path: str | Path) -> tuple[list[tuple[str, str]], list[dict]]:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCUMENT_XML_BYTES:
            raise ValueError("DOCX document.xml превышает допустимый размер")
        document = ElementTree.fromstring(archive.read(info))
    paragraphs = []
    for paragraph in document.findall(f".//{{{namespace}}}body/{{{namespace}}}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{{{namespace}}}t":
                parts.append(node.text or "")
            elif node.tag == f"{{{namespace}}}tab":
                parts.append("\t")
            elif node.tag == f"{{{namespace}}}br":
                parts.append("\n")
        paragraphs.append("".join(parts).strip())
    entries: list[tuple[str, str]] = []
    issues: list[dict] = []
    current_question: str | None = None
    answer_lines: list[str] = []
    saw_answer_marker = False

    def finish() -> None:
        nonlocal current_question, answer_lines, saw_answer_marker
        if current_question is None:
            return
        answer = "\n".join(line for line in answer_lines if line).strip()
        entries.append((current_question, answer))
        if not saw_answer_marker:
            issues.append({"question": current_question, "type": "missing_answer_marker", "source_text": answer})
        current_question, answer_lines, saw_answer_marker = None, [], False

    for text in paragraphs:
        question_match = QUESTION_RE.match(text)
        if question_match:
            finish()
            current_question = question_match.group(1).strip()
            continue
        if current_question is None or not text:
            continue
        answer_match = ANSWER_RE.match(text)
        if answer_match and not answer_lines:
            saw_answer_marker = True
            if answer_match.group(1).strip():
                answer_lines.append(answer_match.group(1).strip())
        else:
            answer_lines.append(text)
    finish()
    return entries, issues


def import_faq(docx_path="chat_bot.docx", db_path="data/bot.db", export_path="data/faq_export.json", report_path="data/import_report.json") -> int:
    pairs, issues = extract_faq(docx_path)
    connection = connect(db_path)
    repository = FAQRepository(connection)
    for question, answer in pairs:
        if question not in METADATA:
            raise ValueError(f"Нет проверенных метаданных для вопроса: {question}")
        if not answer:
            raise ValueError(f"Пустой ответ: {question}")
        category, keywords, aliases = METADATA[question]
        repository.upsert(FAQ(category, question, answer, keywords, aliases))
    connection.commit()
    records = repository.all()
    export_fields = ("id", "category", "question", "answer", "keywords", "aliases", "priority", "active")
    Path(export_path).parent.mkdir(parents=True, exist_ok=True)
    Path(export_path).write_text(json.dumps([{k: row[k] for k in export_fields} for row in records], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # This phrase is retained verbatim; it appears grammatically unclear in the source.
    unclear_fragments = (
        "Оригиналы справок с места учебы обучающиеся могут завтра в деканата института",
        "в электроном атласе",
    )
    for question, answer in pairs:
        for fragment in unclear_fragments:
            if fragment in answer:
                issues.append({"question": question, "type": "potential_source_error", "source_text": fragment})
    Path(report_path).write_text(json.dumps({"faq_count": len(records), "issues": issues}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    connection.close()
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", default="chat_bot.docx")
    parser.add_argument("--db", default="data/bot.db")
    args = parser.parse_args()
    print(f"Импортировано FAQ: {import_faq(args.docx, args.db)}")


if __name__ == "__main__":
    main()
