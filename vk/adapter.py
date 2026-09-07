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
from .keyboards import bot_keyboard, clarification_keyboard, operator_keyboard
from .rate_limit import RateLimiter

logger = logging.getLogger(__name__)
TOO_LONG_TEXT = "Сообщение слишком длинное. Пожалуйста, сформулируйте вопрос короче."
RATE_LIMIT_TEXT = "Слишком много сообщений. Пожалуйста, попробуйте немного позже."
CALLBACK_REJECTED_TEXT = "Этот вариант больше недоступен. Пожалуйста, задайте вопрос снова."
CALLBACK_REJECTED_ACK = "Кнопка устарела. Попробуйте ещё раз."


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
                logger.warning("Temporary VK send failure event_id=%s retry=%s", event.event_id, attempt + 1)
                self.sleep(delay)

    def _dispatch(self, event: IncomingVKMessage, response) -> AdapterResult:
        if response.type is ResponseType.OPERATOR_MODE:
            return AdapterResult(AdapterStatus.NO_AUTOREPLY, response.type.value)
        keyboard = None
        if response.type is ResponseType.CLARIFICATION:
            keyboard = clarification_keyboard(response.options)
        elif response.type in {ResponseType.FAQ_ANSWER, ResponseType.NOT_FOUND, ResponseType.SWITCHED_TO_BOT}:
            keyboard = operator_keyboard()
        elif response.type is ResponseType.SWITCHED_TO_OPERATOR:
            keyboard = bot_keyboard()
        self._send(event, response.text, keyboard)
        logger.info("VK response event_id=%s user_id=%s type=%s faq_id=%s confidence=%s", event.event_id, event.user_id, response.type.value, response.faq_id, response.confidence.value if response.confidence else None)
        return AdapterResult(AdapterStatus.PROCESSED, response.type.value)

    def _ack_callback(self, event: IncomingVKMessage, text: str) -> None:
        try:
            self.client.answer_callback(event.callback_event_id or "", event.user_id, event.peer_id, text)
        except (TemporaryVKError, PermanentVKError, TimeoutError):
            logger.exception("VK callback acknowledgement failed event_id=%s user_id=%s", event.event_id, event.user_id)
        except Exception:
            logger.exception("Unexpected callback acknowledgement failure event_id=%s user_id=%s", event.event_id, event.user_id)

    def _callback(self, event: IncomingVKMessage, logic: BotLogic):
        payload = event.payload or {}
        action = payload.get("action")
        logged_action = action if isinstance(action, str) and action in {"operator", "bot", "clarification"} else "invalid"
        logger.info("VK callback event_id=%s user_id=%s action=%s", event.event_id, event.user_id, logged_action)
        if isinstance(action, str) and action in {"operator", "bot"} and set(payload) == {"action"}:
            response = logic.handle_mode_callback(event.user_id, action)
            return response, "Диалог передан оператору" if action == "operator" else "Автоматический помощник включён"
        faq_id = payload.get("faq_id")
        nonce = payload.get("nonce")
        if (action != "clarification" or set(payload) != {"action", "faq_id", "nonce"}
                or type(faq_id) is not int or faq_id <= 0
                or not isinstance(nonce, str) or not nonce or len(nonce) > 128):
            logger.warning("Rejected callback event_id=%s user_id=%s reason=invalid_payload", event.event_id, event.user_id)
            return None, CALLBACK_REJECTED_ACK
        response = logic.select_clarification(event.user_id, faq_id, nonce)
        if response.type is ResponseType.FAQ_ANSWER:
            return response, "Ответ выбран"
        logger.warning("Rejected callback event_id=%s user_id=%s reason=stale_or_forbidden", event.event_id, event.user_id)
        return response, CALLBACK_REJECTED_ACK

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
            logger.exception("Unable to open SQLite database event_id=%s", event.event_id)
            return AdapterResult(AdapterStatus.ERROR)
        dedup = EventDeduplicator(connection, self.config.event_dedup_ttl_seconds)
        try:
            if not dedup.claim(event.event_id):
                return AdapterResult(AdapterStatus.DUPLICATE)
            if not self.rate_limiter.allow(event.user_id):
                logger.warning("VK rate limit event_id=%s user_id=%s", event.event_id, event.user_id)
                if event.raw_type == "message_event":
                    self._ack_callback(event, CALLBACK_REJECTED_ACK)
                else:
                    self._send(event, RATE_LIMIT_TEXT)
                dedup.finish(event.event_id)
                return AdapterResult(AdapterStatus.RATE_LIMITED)
            if len(event.text) > self.config.max_input_length:
                self._send(event, TOO_LONG_TEXT)
                dedup.finish(event.event_id)
                return AdapterResult(AdapterStatus.INPUT_TOO_LONG)

            logic = BotLogic(connection)
            if event.raw_type == "message_event":
                response, acknowledgement = self._callback(event, logic)
            else:
                response, acknowledgement = logic.handle_message(event.user_id, event.text), None
            if response is None:
                result = AdapterResult(AdapterStatus.INVALID_EVENT)
            else:
                try:
                    result = self._dispatch(event, response)
                finally:
                    if acknowledgement is not None:
                        self._ack_callback(event, acknowledgement)
            if response is None and acknowledgement is not None:
                self._ack_callback(event, acknowledgement)
            dedup.finish(event.event_id)
            return result
        except sqlite3.OperationalError:
            connection.rollback()
            logger.exception("SQLite operational error event_id=%s", event.event_id)
            if event.raw_type == "message_event":
                self._ack_callback(event, CALLBACK_REJECTED_ACK)
            return AdapterResult(AdapterStatus.ERROR)
        except PermanentVKError:
            logger.exception("Permanent VK API error event_id=%s", event.event_id)
            dedup.finish(event.event_id, "FAILED")
            return AdapterResult(AdapterStatus.ERROR)
        except (TemporaryVKError, TimeoutError):
            logger.exception("Temporary VK API error event_id=%s", event.event_id)
            dedup.finish(event.event_id, "FAILED")
            return AdapterResult(AdapterStatus.ERROR)
        except Exception:
            connection.rollback()
            logger.exception("Unexpected adapter error event_id=%s", event.event_id)
            return AdapterResult(AdapterStatus.ERROR)
        finally:
            connection.close()
