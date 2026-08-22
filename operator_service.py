"""User-mode transitions kept separate from transport concerns."""

import logging

from user_repository import UserRepository

logger = logging.getLogger(__name__)


class OperatorService:
    def __init__(self, users: UserRepository):
        self.users = users

    def switch_to_operator(self, vk_user_id: int) -> None:
        self.users.set_user_mode(vk_user_id, "operator")
        logger.info("User %s switched to operator mode", vk_user_id)

    def switch_to_bot(self, vk_user_id: int) -> None:
        self.users.set_user_mode(vk_user_id, "bot")
        logger.info("User %s switched to bot mode", vk_user_id)

    def is_operator_mode(self, vk_user_id: int) -> bool:
        return self.users.get_user_mode(vk_user_id) == "operator"
