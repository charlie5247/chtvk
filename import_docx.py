"""Import FAQ entries from chat_bot.docx and export a reviewable JSON copy."""

import argparse
import json
import re
from difflib import SequenceMatcher
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

# Reviewed source-to-source identity changes.  Keeping this separate from METADATA
# makes a rename explicit and prevents a changed wording from becoming a new FAQ.
RENAMED_QUESTIONS = {
    "Как связаться с преподавателем?": "Как связаться или найти преподавателя?",
}

DIFF_STATUSES = (
    "ADDED", "MODIFIED_ANSWER", "MODIFIED_QUESTION", "MODIFIED_METADATA",
    "REMOVED_FROM_SOURCE", "UNCHANGED", "POSSIBLE_DUPLICATE", "AMBIGUOUS",
)


def normalize_question(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", value.lower().replace("ё", "е")).split())


def metadata_for(question: str) -> tuple[str, list[str], list[str]]:
    """Return reviewed metadata, resolving an explicitly reviewed rename."""
    metadata_question = next(
        (old for old, new in RENAMED_QUESTIONS.items() if new == question), question
    )
    if metadata_question not in METADATA:
        raise ValueError(f"Нет проверенных метаданных для вопроса: {question}")
    category, keywords, aliases = METADATA[metadata_question]
    aliases = list(aliases)
    if metadata_question != question and normalize_question(metadata_question) not in {
        normalize_question(alias) for alias in aliases
    }:
        aliases.append(metadata_question)
    return category, list(keywords), aliases


def build_source_diff(
    old_pairs: list[tuple[str, str]], new_pairs: list[tuple[str, str]]
) -> dict[str, list[dict]]:
    """Build a deterministic, reviewable diff without guessing FAQ identity."""
    result = {status: [] for status in DIFF_STATUSES}
    old = dict(old_pairs)
    new = dict(new_pairs)
    consumed_old: set[str] = set()
    consumed_new: set[str] = set()

    for old_question, new_question in RENAMED_QUESTIONS.items():
        if old_question in old and new_question in new:
            old_meta = metadata_for(old_question)
            new_meta = metadata_for(new_question)
            result["MODIFIED_QUESTION"].append({
                "old_question": old_question,
                "new_question": new_question,
                "answer_changed": old[old_question] != new[new_question],
                "metadata_changed": old_meta != new_meta,
            })
            consumed_old.add(old_question)
            consumed_new.add(new_question)

    for question in sorted(old.keys() & new.keys()):
        old_meta = metadata_for(question)
        new_meta = metadata_for(question)
        if old[question] != new[question]:
            result["MODIFIED_ANSWER"].append({"question": question})
        elif old_meta != new_meta:
            result["MODIFIED_METADATA"].append({"question": question})
        else:
            result["UNCHANGED"].append({"question": question})
        consumed_old.add(question)
        consumed_new.add(question)

    unmatched_old = sorted(set(old) - consumed_old)
    unmatched_new = sorted(set(new) - consumed_new)
    for question in unmatched_new:
        # Similar wording is a review item, never an automatically inferred rename.
        candidates = [
            candidate for candidate in unmatched_old
            if SequenceMatcher(None, normalize_question(candidate), normalize_question(question)).ratio() >= 0.72
        ]
        if candidates:
            result["AMBIGUOUS"].append({"new_question": question, "old_candidates": candidates})
        else:
            result["ADDED"].append({"question": question})
    ambiguous_old = {q for item in result["AMBIGUOUS"] for q in item["old_candidates"]}
    for question in unmatched_old:
        if question not in ambiguous_old:
            result["REMOVED_FROM_SOURCE"].append({"question": question})

    variants: dict[str, set[str]] = {}
    for question in new:
        _, _, aliases = metadata_for(question)
        for value in (question, *aliases):
            variants.setdefault(normalize_question(value), set()).add(question)
    for normalized, owners in sorted(variants.items()):
        if normalized and len(owners) > 1:
            result["POSSIBLE_DUPLICATE"].append({
                "normalized_text": normalized, "questions": sorted(owners)
            })
    result["summary"] = {status: len(result[status]) for status in DIFF_STATUSES}
    return result


def validate_stored_variants(connection) -> None:
    """Reject normalized question duplicates and aliases owned by multiple FAQ rows."""
    owners: dict[str, set[int]] = {}
    questions: dict[str, set[int]] = {}
    for row in connection.execute("SELECT id,question,aliases FROM faq"):
        normalized = normalize_question(row["question"])
        questions.setdefault(normalized, set()).add(row["id"])
        for value in (row["question"], *json.loads(row["aliases"])):
            owners.setdefault(normalize_question(value), set()).add(row["id"])
    if any(len(ids) > 1 for ids in questions.values()):
        raise ValueError("База содержит дублирующиеся нормализованные вопросы")
    if any(normalized and len(ids) > 1 for normalized, ids in owners.items()):
        raise ValueError("База содержит конфликтующие aliases")


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


def import_faq(docx_path="chat_bot.docx", db_path="data/bot.db", export_path="data/faq_export.json", report_path="data/import_report.json", diff_path=None, old_docx_path="chat_bot.docx") -> int:
    pairs, issues = extract_faq(docx_path)
    if len({normalize_question(question) for question, _ in pairs}) != len(pairs):
        raise ValueError("Источник содержит дублирующиеся нормализованные вопросы")
    prepared = []
    for question, answer in pairs:
        if not answer:
            raise ValueError(f"Пустой ответ: {question}")
        prepared.append((question, answer, metadata_for(question)))

    old_pairs, _ = extract_faq(old_docx_path)
    source_diff = build_source_diff(old_pairs, pairs)
    if diff_path is not None:
        Path(diff_path).parent.mkdir(parents=True, exist_ok=True)
        Path(diff_path).write_text(json.dumps(source_diff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if source_diff["AMBIGUOUS"] or source_diff["POSSIBLE_DUPLICATE"]:
        raise ValueError("Обновление заблокировано: обнаружены неоднозначные или конфликтующие FAQ")

    connection = connect(db_path)
    repository = FAQRepository(connection)
    connection.commit()
    imported = updated = added = preserved_ids = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        for question, answer, metadata in prepared:
            category, keywords, aliases = metadata
            old_question = next((old for old, new in RENAMED_QUESTIONS.items() if new == question), None)
            current = connection.execute("SELECT * FROM faq WHERE question=? COLLATE NOCASE", (question,)).fetchone()
            renamed = connection.execute("SELECT * FROM faq WHERE question=? COLLATE NOCASE", (old_question,)).fetchone() if old_question else None
            if current is not None and renamed is not None and current["id"] != renamed["id"]:
                raise ValueError(f"Конфликт переименования FAQ: {old_question} -> {question}")
            if renamed is not None and current is None:
                connection.execute(
                    "UPDATE faq SET question=?, category=?, answer=?, keywords=?, aliases=?, updated_at=? WHERE id=?",
                    (question, category, answer, json.dumps(keywords, ensure_ascii=False),
                     json.dumps(aliases, ensure_ascii=False), FAQ(category, question, answer, keywords, aliases).updated_at, renamed["id"]),
                )
                updated += 1
                preserved_ids += 1
            else:
                existing_id = current["id"] if current is not None else None
                if existing_id is None:
                    repository.upsert(FAQ(category, question, answer, keywords, aliases))
                    added += 1
                else:
                    preserved_ids += 1
                    stored = (
                        current["category"], current["answer"], json.loads(current["keywords"]),
                        json.loads(current["aliases"]),
                    )
                    if stored != (category, answer, keywords, aliases):
                        repository.upsert(FAQ(category, question, answer, keywords, aliases))
                        updated += 1
            imported += 1
        validate_stored_variants(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
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
    Path(report_path).write_text(json.dumps({
        "faq_count": len(records),
        "import": {"imported": imported, "updated": updated, "added": added, "preserved_ids": preserved_ids},
        "source_diff": source_diff["summary"],
        "issues": issues,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    connection.close()
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", default="chat_bot.docx")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--diff", default="data/faq_update_diff.json")
    args = parser.parse_args()
    print(f"Импортировано FAQ: {import_faq(args.docx, args.db, diff_path=args.diff)}")


if __name__ == "__main__":
    main()
