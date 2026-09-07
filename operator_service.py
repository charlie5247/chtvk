"""User-mode transitions kept separate from transport concerns."""

import logging

from user_repository import UserRepository

logger = logging.getLogger(__name__)


class OperatorService:
    def __init__(self, users: UserRepository):
        self.users = users

    def switch_to_operator(self, vk_user_id: int) -> None:
        previous = self.users.get_user_mode(vk_user_id)
        self.users.set_user_mode(vk_user_id, "operator")
        if previous != "operator":
            logger.info("User %s mode transition bot -> operator", vk_user_id)

    def switch_to_bot(self, vk_user_id: int) -> None:
        previous = self.users.get_user_mode(vk_user_id)
        self.users.set_user_mode(vk_user_id, "bot")
        if previous != "bot":
            logger.info("User %s mode transition operator -> bot", vk_user_id)

    def is_operator_mode(self, vk_user_id: int) -> bool:
        return self.users.get_user_mode(vk_user_id) == "operator"
