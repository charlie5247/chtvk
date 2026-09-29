"""Safe transport boundary from VK events to the existing BotLogic."""

import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from bot_logic import BotLogic, ResponseType

from .client import VKClientProtocol
from .config import VKConfig
from .deduplication import EventDeduplicator
from .events import IncomingVKMessage, parse_event
from .exceptions import PermanentVKError, TemporaryVKError, ValidationError
from .keyboards import bot_keyboard, clarification_keyboard, faq_menu_keyboard, operator_keyboard
from .rate_limit import RateLimiter

logger = logging.getLogger(__name__)
TOO_LONG_TEXT = "Сообщение слишком длинное. Пожалуйста, сформулируйте вопрос короче."
RATE_LIMIT_TEXT = "Слишком много сообщений. Пожалуйста, попробуйте немного позже."
CALLBACK_REJECTED_TEXT = "Этот вариант больше недоступен. Пожалуйста, задайте вопрос снова."
STALE_CALLBACK_TEXT = "Кнопка устарела. Попробуйте ещё раз."
STALE_MENU_TEXT = "Список устарел. Откройте вопросы заново."


class AdapterStatus(str, Enum):
    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    RATE_LIMITED = "RATE_LIMITED"
    INPUT_TOO_LONG = "INPUT_TOO_LONG"
    INVALID_EVENT = "INVALID_EVENT"
    UNSUPPORTED_EVENT = "UNSUPPORTED_EVENT"
    NO_AUTOREPLY = "NO_AUTOREPLY"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: AdapterStatus
    response_type: str | None = None


class VKAdapter:
    def __init__(self, connection_factory: Callable[[], sqlite3.Connection], client: VKClientProtocol, config: VKConfig, sleep: Callable[[float], None] = time.sleep):
        self.connection_factory = connection_factory
        self.client = client
        self.config = config
        self.rate_limiter = RateLimiter(config.rate_limit_per_minute)
        self.sleep = sleep

    def _send(self, event: IncomingVKMessage, text: str, keyboard: dict | None = None) -> None:
        for attempt in range(3):
            try:
                self.client.send_message(event.peer_id, text, keyboard)
                return
            except (TimeoutError, TemporaryVKError):
                if attempt == 2:
                    raise
                delay = 0.1 * (2**attempt) + random.uniform(0, 0.05)
                logger.warning("Temporary VK send failure event_id=%s retry=%s", event.transport_event_id, attempt + 1)
                self.sleep(delay)

    def _dispatch(self, event: IncomingVKMessage, response) -> AdapterResult:
        if response.type is ResponseType.OPERATOR_MODE:
            return AdapterResult(AdapterStatus.NO_AUTOREPLY, response.type.value)
        keyboard = None
        if response.type is ResponseType.CLARIFICATION:
            keyboard = clarification_keyboard(response.options)
        elif response.type is ResponseType.FAQ_MENU:
            keyboard = faq_menu_keyboard(response.options, response.faq_id or 0)
        elif response.type in {ResponseType.FAQ_ANSWER, ResponseType.NOT_FOUND, ResponseType.SWITCHED_TO_BOT}:
            keyboard = operator_keyboard()
        elif response.type is ResponseType.SWITCHED_TO_OPERATOR:
            keyboard = bot_keyboard()
        self._send(event, response.text, keyboard)
        logger.info("VK response event_id=%s user_id=%s type=%s faq_id=%s confidence=%s", event.transport_event_id, event.user_id, response.type.value, response.faq_id, response.confidence.value if response.confidence else None)
        return AdapterResult(AdapterStatus.PROCESSED, response.type.value)

    def _callback(self, event: IncomingVKMessage, logic: BotLogic):
        payload = event.payload or {}
        action = payload.get("action")
        if action == "operator":
            return logic.handle_message(event.user_id, "оператор")
        if action == "bot":
            return logic.handle_message(event.user_id, "вернуться к боту")
        if action == "all_questions":
            return logic.open_faq_menu(event.user_id)
        if action == "faq_page":
            page, nonce = payload.get("page"), payload.get("nonce")
            if type(page) is not int or page < 0 or not isinstance(nonce, str) or not nonce:
                return None
            return logic.open_faq_menu(event.user_id, page, nonce)
        if action == "faq_select":
            return logic.select_faq_menu(event.user_id, payload.get("faq_id"), payload.get("nonce"))
        faq_id = payload.get("faq_id")
        nonce = payload.get("nonce")
        if action != "clarification" or type(faq_id) is not int or faq_id <= 0 or not isinstance(nonce, str) or not nonce:
            logger.warning("Rejected callback event_id=%s user_id=%s", event.transport_event_id, event.user_id)
            return None
        return logic.select_clarification(event.user_id, faq_id, nonce)

    def _answer_callback(self, event: IncomingVKMessage, text: str) -> None:
        # VK's object.event_id is unrelated to the transport id used for deduplication.
        self.client.answer_callback(event.callback_event_id or "", event.user_id, event.peer_id, text)

    def handle_event(self, raw_event: object) -> AdapterResult:
        try:
            event = parse_event(raw_event)
        except ValidationError:
            logger.warning("Rejected malformed VK event")
            return AdapterResult(AdapterStatus.INVALID_EVENT)
        if event is None:
            return AdapterResult(AdapterStatus.UNSUPPORTED_EVENT)

        try:
            connection = self.connection_factory()
        except sqlite3.OperationalError:
            logger.exception("Unable to open SQLite database event_id=%s", event.transport_event_id)
            return AdapterResult(AdapterStatus.ERROR)
        dedup = EventDeduplicator(connection, self.config.event_dedup_ttl_seconds)
        try:
            if not dedup.claim(event.transport_event_id):
                return AdapterResult(AdapterStatus.DUPLICATE)
            if event.outgoing:
                # Only a direct message authored by this community can be an
                # operator command. Everything else fails closed and is ignored.
                valid_sender = self.config.group_id is not None and event.sender_id == -self.config.group_id
                direct_peer = event.peer_id == event.user_id and 0 < event.peer_id < 2_000_000_000
                command = event.text.strip().casefold()
                response = None
                if valid_sender and direct_peer and command in {"/бот", "/bot"}:
                    response = BotLogic(connection).operator_return_to_bot(event.user_id)
                result = self._dispatch(event, response) if response is not None else AdapterResult(AdapterStatus.NO_AUTOREPLY)
                dedup.finish(event.transport_event_id)
                return result
            # Callback events must always reach sendMessageEventAnswer; throttling
            # them here would leave a permanent spinner in the VK client.
            if event.raw_type == "message_new" and not self.rate_limiter.allow(event.user_id):
                logger.warning("VK rate limit event_id=%s user_id=%s", event.transport_event_id, event.user_id)
                self._send(event, RATE_LIMIT_TEXT)
                dedup.finish(event.transport_event_id)
                return AdapterResult(AdapterStatus.RATE_LIMITED)
            if len(event.text) > self.config.max_input_length:
                self._send(event, TOO_LONG_TEXT)
                dedup.finish(event.transport_event_id)
                return AdapterResult(AdapterStatus.INPUT_TOO_LONG)

            logic = BotLogic(connection)
            response = self._callback(event, logic) if event.raw_type == "message_event" else logic.handle_message(event.user_id, event.text)
            if event.raw_type == "message_event":
                action = (event.payload or {}).get("action")
                if response is None or response.type is ResponseType.OPERATOR_MODE:
                    snackbar = STALE_MENU_TEXT if action in {"all_questions", "faq_page", "faq_select"} else STALE_CALLBACK_TEXT
                elif response.type is ResponseType.FAQ_MENU:
                    snackbar = "Список вопросов открыт" if action == "all_questions" else "Страница открыта"
                elif action == "faq_select":
                    snackbar = "Ответ выбран"
                elif response.type is ResponseType.NOT_FOUND:
                    snackbar = STALE_CALLBACK_TEXT
                elif response.type is ResponseType.SWITCHED_TO_OPERATOR:
                    snackbar = "Диалог передан оператору"
                elif response.type is ResponseType.SWITCHED_TO_BOT:
                    snackbar = "Автоматический помощник включён"
                else:
                    snackbar = "Ответ выбран"
                self._answer_callback(event, snackbar)
            if response is None:
                self._send(event, CALLBACK_REJECTED_TEXT)
                result = AdapterResult(AdapterStatus.INVALID_EVENT)
            else:
                result = self._dispatch(event, response)
            dedup.finish(event.transport_event_id)
            return result
        except sqlite3.OperationalError:
            connection.rollback()
            logger.exception("SQLite operational error event_id=%s", event.transport_event_id)
            return AdapterResult(AdapterStatus.ERROR)
        except PermanentVKError:
            logger.exception("Permanent VK API error event_id=%s", event.transport_event_id)
            dedup.finish(event.transport_event_id, "FAILED")
            return AdapterResult(AdapterStatus.ERROR)
        except (TemporaryVKError, TimeoutError):
            logger.exception("Temporary VK API error event_id=%s", event.transport_event_id)
            dedup.finish(event.transport_event_id, "FAILED")
            return AdapterResult(AdapterStatus.ERROR)
        except Exception:
            connection.rollback()
            logger.exception("Unexpected adapter error event_id=%s", event.transport_event_id)
            return AdapterResult(AdapterStatus.ERROR)
        finally:
            connection.close()
