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
SWITCHED_TO_OPERATOR_TEXT = (
    "Диалог передан оператору. Напишите ваш вопрос одним сообщением.\n"
    "Пока диалог передан оператору, автоматические ответы отключены."
)
SWITCHED_TO_BOT_TEXT = "Автоматический помощник снова включён. Можете задать вопрос."
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
            self.operator.switch_to_bot(vk_user_id)
            return self._respond(vk_user_id, BotResponse(ResponseType.SWITCHED_TO_BOT, SWITCHED_TO_BOT_TEXT))
        if normalized in OPERATOR_COMMANDS:
            self.interactions.clear_clarification(vk_user_id)
            self.operator.switch_to_operator(vk_user_id)
            return self._respond(vk_user_id, BotResponse(ResponseType.SWITCHED_TO_OPERATOR, SWITCHED_TO_OPERATOR_TEXT))
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

    def handle_mode_callback(self, vk_user_id: int, mode: str) -> BotResponse:
        """Apply a server-controlled callback transition without inventing user text."""
        if mode not in {"bot", "operator"}:
            raise ValueError("Некорректный callback режима")
        try:
            with self.connection:
                self.users.get_or_create_user(vk_user_id)
                self.interactions.clear_clarification(vk_user_id)
                if mode == "operator":
                    self.operator.switch_to_operator(vk_user_id)
                    response = BotResponse(ResponseType.SWITCHED_TO_OPERATOR, SWITCHED_TO_OPERATOR_TEXT)
                else:
                    self.operator.switch_to_bot(vk_user_id)
                    response = BotResponse(ResponseType.SWITCHED_TO_BOT, SWITCHED_TO_BOT_TEXT)
                return self._respond(vk_user_id, response)
        except sqlite3.Error:
            logger.exception("SQLite failure while handling mode callback for user %s", vk_user_id)
            raise

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


def handle_message(vk_user_id: int, text: str, connection: sqlite3.Connection) -> BotResponse:
    return BotLogic(connection).handle_message(vk_user_id, text)
