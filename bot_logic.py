"""Transport-independent orchestration for chatbot messages."""

import sqlite3
import logging
import secrets
from dataclasses import dataclass, field
from enum import Enum

from faq_search import Confidence, FAQSearch, SearchSettings, normalize_text
from interaction_repository import InteractionRepository
from operator_service import OperatorService
from user_repository import UserRepository


class ResponseType(str, Enum):
    FAQ_ANSWER = "FAQ_ANSWER"
    CLARIFICATION = "CLARIFICATION"
    NOT_FOUND = "NOT_FOUND"
    SWITCHED_TO_OPERATOR = "SWITCHED_TO_OPERATOR"
    SWITCHED_TO_BOT = "SWITCHED_TO_BOT"
    OPERATOR_MODE = "OPERATOR_MODE"
    FAQ_MENU = "FAQ_MENU"


@dataclass(frozen=True, slots=True)
class BotResponse:
    type: ResponseType
    text: str
    faq_id: int | None = None
    options: list[dict] = field(default_factory=list)
    confidence: Confidence | None = None


OPERATOR_COMMANDS = {"оператор", "связаться с оператором", "позвать оператора", "живой оператор"}
BOT_COMMANDS = {"вернуться к боту", "бот", "вернуться в меню"}
NOT_FOUND_TEXT = "Я не нашёл точного ответа в базе. Вы можете сформулировать вопрос иначе или обратиться к оператору."
OPERATOR_TEXT = (
    "Диалог передан оператору. Напишите ваш вопрос одним сообщением.\n"
    "Пока диалог передан оператору, автоматические ответы отключены."
)
BOT_TEXT = "Автоматический помощник снова включён. Можете задать вопрос."
logger = logging.getLogger(__name__)


class BotLogic:
    def __init__(self, connection: sqlite3.Connection, settings: SearchSettings | None = None):
        self.connection = connection
        self.search_service = FAQSearch(connection, settings)
        self.users = UserRepository(connection, auto_commit=False)
        self.operator = OperatorService(self.users)
        self.interactions = InteractionRepository(connection, auto_commit=False)

    def _respond(self, vk_user_id: int, response: BotResponse) -> BotResponse:
        if response.text:
            self.interactions.save_message(vk_user_id, "outgoing", response.text, response.type.value, response.faq_id, response.confidence.value if response.confidence else None)
        return response

    def handle_message(self, vk_user_id: int, text: str) -> BotResponse:
        if not isinstance(text, str):
            logger.warning("Rejected non-text message for user %s", vk_user_id)
            text = ""
        try:
            with self.connection:
                return self._handle_message(vk_user_id, text)
        except sqlite3.Error:
            logger.exception("SQLite failure while handling message for user %s", vk_user_id)
            raise

    def _handle_message(self, vk_user_id: int, text: str) -> BotResponse:
        self.users.get_or_create_user(vk_user_id)
        self.interactions.save_message(vk_user_id, "incoming", text)
        normalized = normalize_text(text)

        # This check intentionally precedes operator-mode suppression.
        if normalized in BOT_COMMANDS:
            self.interactions.clear_clarification(vk_user_id)
            self.interactions.clear_faq_menu(vk_user_id)
            self.operator.switch_to_bot(vk_user_id)
            return self._respond(vk_user_id, BotResponse(ResponseType.SWITCHED_TO_BOT, BOT_TEXT))
        if normalized in OPERATOR_COMMANDS:
            self.interactions.clear_clarification(vk_user_id)
            self.interactions.clear_faq_menu(vk_user_id)
            self.operator.switch_to_operator(vk_user_id)
            return self._respond(vk_user_id, BotResponse(ResponseType.SWITCHED_TO_OPERATOR, OPERATOR_TEXT))
        if self.operator.is_operator_mode(vk_user_id):
            return BotResponse(ResponseType.OPERATOR_MODE, "")

        self.interactions.clear_clarification(vk_user_id)
        result = self.search_service.search(text)
        if result.confidence is Confidence.HIGH and result.faq:
            return self._respond(vk_user_id, BotResponse(ResponseType.FAQ_ANSWER, result.faq["answer"], result.faq["id"], confidence=result.confidence))
        if result.confidence is Confidence.MEDIUM and result.faq:
            options = [{"faq_id": candidate.faq["id"], "question": candidate.faq["question"]} for candidate in result.alternatives[:3]]
            nonce = secrets.token_urlsafe(18)
            options = [{**option, "nonce": nonce} for option in options]
            self.interactions.save_clarification(vk_user_id, [option["faq_id"] for option in options], nonce)
            text_out = "Возможно, вы имели в виду:\n" + "\n".join(f"{index}. {option['question']}" for index, option in enumerate(options, 1))
            return self._respond(vk_user_id, BotResponse(ResponseType.CLARIFICATION, text_out, options=options, confidence=result.confidence))

        self.interactions.save_unknown(vk_user_id, text, normalized, result.score)
        logger.warning("FAQ not found for user %s (score %.3f)", vk_user_id, result.score)
        return self._respond(vk_user_id, BotResponse(ResponseType.NOT_FOUND, NOT_FOUND_TEXT, confidence=Confidence.LOW))

    def select_clarification(self, vk_user_id: int, faq_id: int, nonce: str = "") -> BotResponse:
        """Return only a stored answer after the caller selects an offered FAQ id."""
        try:
            with self.connection:
                if self.operator.is_operator_mode(vk_user_id):
                    return BotResponse(ResponseType.OPERATOR_MODE, "")
                allowed = self.interactions.consume_clarification(vk_user_id, faq_id, nonce)
                faq = self.search_service.repository.get_active(faq_id) if allowed else None
                if faq is None:
                    return self._respond(vk_user_id, BotResponse(ResponseType.NOT_FOUND, NOT_FOUND_TEXT, confidence=Confidence.LOW))
                return self._respond(vk_user_id, BotResponse(ResponseType.FAQ_ANSWER, faq["answer"], faq["id"], confidence=Confidence.HIGH))
        except sqlite3.Error:
            logger.exception("SQLite failure while selecting clarification for user %s", vk_user_id)
            raise

    def open_faq_menu(self, vk_user_id: int, page: int = 0, nonce: str = "") -> BotResponse | None:
        if type(page) is not int or page < 0:
            return None
        try:
            with self.connection:
                self.users.get_or_create_user(vk_user_id)
                if self.operator.is_operator_mode(vk_user_id):
                    return BotResponse(ResponseType.OPERATOR_MODE, "")
                if nonce:
                    allowed = self.interactions.validate_faq_menu(vk_user_id, nonce)
                    if allowed is None:
                        return None
                    faqs = [faq for faq_id in allowed if (faq := self.search_service.repository.get_active(faq_id))]
                else:
                    faqs = self.search_service.repository.active()
                    nonce = secrets.token_urlsafe(18)
                    self.interactions.save_faq_menu(vk_user_id, [faq["id"] for faq in faqs], nonce)
                if page * 5 >= max(len(faqs), 1):
                    return None
                options = [{"faq_id": faq["id"], "question": faq["question"], "nonce": nonce} for faq in faqs]
                return BotResponse(ResponseType.FAQ_MENU, "Выберите интересующий вопрос:", options=options, faq_id=page)
        except sqlite3.Error:
            logger.exception("SQLite failure while opening FAQ menu for user %s", vk_user_id)
            raise

    def select_faq_menu(self, vk_user_id: int, faq_id: int, nonce: str) -> BotResponse | None:
        if type(faq_id) is not int or faq_id <= 0 or not isinstance(nonce, str) or not nonce:
            return None
        with self.connection:
            if self.operator.is_operator_mode(vk_user_id):
                return BotResponse(ResponseType.OPERATOR_MODE, "")
            allowed = self.interactions.validate_faq_menu(vk_user_id, nonce)
            if allowed is None or faq_id not in allowed:
                return None
            faq = self.search_service.repository.get_active(faq_id)
            if faq is None:
                return None
            self.interactions.clear_faq_menu(vk_user_id)
            return self._respond(vk_user_id, BotResponse(ResponseType.FAQ_ANSWER, faq["answer"], faq_id, confidence=Confidence.HIGH))

    def operator_return_to_bot(self, vk_user_id: int) -> BotResponse | None:
        with self.connection:
            self.users.get_or_create_user(vk_user_id)
            if not self.operator.is_operator_mode(vk_user_id):
                return None
            self.interactions.clear_clarification(vk_user_id)
            self.interactions.clear_faq_menu(vk_user_id)
            self.operator.switch_to_bot(vk_user_id)
            return self._respond(vk_user_id, BotResponse(ResponseType.SWITCHED_TO_BOT, BOT_TEXT))


def handle_message(vk_user_id: int, text: str, connection: sqlite3.Connection) -> BotResponse:
    return BotLogic(connection).handle_message(vk_user_id, text)
